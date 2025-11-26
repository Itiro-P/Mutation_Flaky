import json
import csv
from pathlib import Path
from typing import List, Dict, Optional
import re
import shutil
import sys
import click
from bs4 import BeautifulSoup
import matplotlib.pyplot as plt
import pandas as pd

# ============================================================================
# Conteúdo de mutation.csv:
# sourceFile          → Arquivo mutado
# mutatedClass        → Classe
# mutator             → Tipo de mutação (Operador de mutação)
# mutatedMethod       → Método
# lineNumber          → Linha do código
# status              → KILLED, SURVIVED, NO_COVERAGE
# killingTest         → Qual teste matou a mutação
#
# Conteúdo de metadata.json:
# index               → Posição do commit na análise
# commit              → Identificação do commit em hexadecimal
# info:
#   message           → Nome do commit
#   date              → Data do commit
# time_elapsed:
#   setup_time        → Tempo para configurar Docker (segundos)
#   pit_time          → Tempo para mutações (segundos)
#   cleanup_time      → Tempo para limpar e remover Docker (segundos)
# report_dir          → Diretório do relatório
# ============================================================================

FOLDERS_TO_IGNORE = {"docker-temp", "tmp-wt"}


class Commit:
    """Representa um commit com seus dados de mutação"""
    
    def __init__(self, index: int, commit_hash: str, metadata: Dict = None):
        self.index = index
        self.commit_hash = commit_hash
        self.metadata = metadata or {}
        self.mutations_df: Optional[pd.DataFrame] = None
        self.summary: Dict = {}
    
    def load_mutations_csv(self, csv_path: Path) -> bool:
        """Carregar dados de mutações do CSV"""
        try:
            # Colunas esperadas (PITest CSV sem header)
            columns = [
                'sourceFile',
                'mutatedClass',
                'mutator',
                'mutatedMethod',
                'lineNumber',
                'status',
                'killingTest'
            ]
            
            # Ler CSV sem header
            df = pd.read_csv(csv_path, header=None, names=columns)
            
            # DEBUG: Mostrar colunas
            print(f"      Colunas carregadas: {list(df.columns)}")
            print(f"      Shape: {df.shape}")
            print(f"      Primeiras linhas:")
            print(df.head(2))
            
            self.mutations_df = df
            self._calculate_summary()
            return True
        except Exception as e:
            print(f"Erro ao ler CSV {csv_path}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _calculate_summary(self):
        """Calcular resumo das mutações"""
        if self.mutations_df is None or len(self.mutations_df) == 0:
            return
        
        df = self.mutations_df
        
        killed = len(df[df['status'] == 'KILLED'])
        survived = len(df[df['status'] == 'SURVIVED'])
        no_coverage = len(df[df['status'] == 'NO_COVERAGE'])
        total = len(df)
        
        # Taxa de kill (apenas killed vs survived, não NO_COVERAGE)
        testavel = killed + survived
        taxa_kill = (killed / testavel * 100) if testavel > 0 else 0
        
        self.summary = {
            'total_mutacoes': total,
            'killed': killed,
            'survived': survived,
            'no_coverage': no_coverage,
            'taxa_kill': taxa_kill,
            'metodos_afetados': df['mutatedMethod'].nunique(),
            'linhas_afetadas': df['lineNumber'].nunique(),
        }
    
    def get_summary(self) -> Dict:
        """Retornar resumo"""
        return self.summary
    
    def __repr__(self):
        return f"Commit({self.index}, {self.commit_hash[:7]}, taxa={self.summary.get('taxa_kill', 0):.1f}%)"


class ReportLoader:
    """Carregador de relatórios PITest"""
    
    def __init__(self, reports_path: Path):
        self.reports_path = reports_path
        self.commits: List[Commit] = []
    
    def load_all_reports(self) -> bool:
        """Carregar todos os relatórios"""
        try:
            print(f"Procurando em: {self.reports_path}\n")
            
            def extract_index(dirname: str) -> int:
                """Extrai o índice do nome do diretório"""
                parts = dirname.split('-')
                if parts[0].isdigit():
                    return int(parts[0])
                return float('inf')
            
            # Procurar commits neste diretório
            commit_dirs = sorted([
                d for d in self.reports_path.iterdir()
                if d.is_dir() and not d.name in FOLDERS_TO_IGNORE
            ], key=lambda x: extract_index(x.name))
            
            if not commit_dirs:
                print("Nenhum diretório de commit encontrado")
                return False
            
            print(f"Encontrados {len(commit_dirs)} commit(s)\n")
            
            for commit_dir in commit_dirs:
                # Extrair índice e hash
                parts = commit_dir.name.split('-', 1)
                if len(parts) != 2:
                    print(f"  Nome inválido (esperado NN-{hash}): {commit_dir.name}")
                    continue
                
                idx, commit_hash = parts
                
                # Validar índice
                if not idx.isdigit():
                    print(f"  Índice inválido: {idx}")
                    continue
                
                # Carregar metadados
                metadata_file = commit_dir / "metadata.json"
                metadata = {}
                if metadata_file.exists():
                    try:
                        with open(metadata_file) as f:
                            metadata = json.load(f)
                    except Exception as e:
                        print(f"  Erro ao ler metadata: {e}")
                
                # Criar objeto Commit
                commit = Commit(int(idx), commit_hash, metadata)
                
                # Carregar CSV de mutações
                csv_file = commit_dir / "mutations.csv"
                if csv_file.exists():
                    if commit.load_mutations_csv(csv_file):
                        self.commits.append(commit)
                        print(f"  {commit}")
                    else:
                        print(f"  Erro ao carregar {commit_dir.name}")
                else:
                    print(f"  mutations.csv não encontrado em {commit_dir.name}")
            
            return len(self.commits) > 0
        
        except Exception as e:
            print(f"Erro ao carregar relatórios: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_all_summaries(self) -> pd.DataFrame:
        """Retornar resumo de todos os commits em DataFrame"""
        summaries = []
        
        for commit in self.commits:
            summary = commit.get_summary()
            summary['index'] = commit.index
            summary['commit'] = commit.commit_hash
            summary['message'] = commit.metadata.get('info', {}).get('message', '')
            summary['date'] = commit.metadata.get('info', {}).get('date', '')
            summaries.append(summary)
        
        return pd.DataFrame(summaries)
    
    def save_consolidated_csv(self, output_path: Path):
        """Salvar resumo consolidado em CSV"""
        df = self.get_all_summaries()
        df.to_csv(output_path, index=False)
        print(f"\nResumo consolidado salvo em: {output_path}")
    

    def load_baseline_mutations(self) -> float:
        """
        Carrega o mutations.csv do diretório raiz e calcula a taxa de kill do baseline.
        Retorna a taxa de kill (float) ou 0.0 em caso de erro.
        """
        baseline_path = self.reports_path / "mutations.csv" 
        
        # Se for executado diretamente na raiz, o baseline deve estar na raiz.
        if not baseline_path.exists():
             baseline_path = Path("mutations.csv").resolve()

        if not baseline_path.exists():
            print(f"\nAviso: mutations.csv de Baseline não encontrado. Procurado em: {self.reports_path.parent / 'mutations.csv'} e {Path('mutations.csv').resolve()}")
            return 0.0
        
        print(f"\nCarregando Baseline de: {baseline_path}")
        
        # Cria um objeto Commit temporário
        baseline_commit = Commit(0, 'baseline')
        
        # Reutiliza o carregador de CSV e o calculador de resumo
        if baseline_commit.load_mutations_csv(baseline_path):
            summary = baseline_commit.get_summary()
            taxa_kill = summary.get('taxa_kill', 0.0)
            print(f"  Baseline Taxa de Kill: {taxa_kill:.2f}%")
            return taxa_kill
        
        return 0.0

    def generate_graphs(self, output_dir: Path, baseline_kill_rate: float, baseline_time: float):
        """Gerar gráficos"""
        if not self.commits:
            print("Nenhum commit para gerar gráficos")
            return
        
        WINDOW_SIZE = 3 # Tamanho da janela de suavização

        tempos = {
            'setup_time': [],
            'pit_time': [], # ESTA É A MÉTRICA QUE QUEREMOS
            'cleanup_time': [],
            'index': []
        }
        
        # Coleta de dados de tempo (necessário para o Gráfico 03 e para a Média)
        for commit in self.commits:
            time_elapsed = commit.metadata.get('time_elapsed', {})
            tempos['setup_time'].append(time_elapsed.get('setup_time', 0))
            tempos['pit_time'].append(time_elapsed.get('pit_time', 0))
            tempos['cleanup_time'].append(time_elapsed.get('cleanup_time', 0))
            tempos['index'].append(commit.index)

        df = self.get_all_summaries()
        df_time = pd.DataFrame(tempos).set_index('index')
        df_time_smoothed = df_time.rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()
        output_dir.mkdir(parents=True, exist_ok=True)
        historical_mean_kill_rate = df['taxa_kill'].mean() if not df.empty else 0.0
        historical_mean_time = df_time['pit_time'].mean() if not df_time.empty else 0.0
        
        # Calcular a média móvel (rolling mean) para as métricas principais
        df['taxa_kill_smoothed'] = df['taxa_kill'].rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()
        df['killed_smoothed'] = df['killed'].rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()
        df['survived_smoothed'] = df['survived'].rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()
        df['total_mutacoes_smoothed'] = df['total_mutacoes'].rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()

        # Gráfico 1: Taxa de kill 
        plt.figure(figsize=(14, 7))

        if baseline_kill_rate > 0.0:
            plt.axhline(
                    y=baseline_kill_rate, 
                    color='red', 
                    linestyle='--', 
                    linewidth=2, 
                    label=f'Baseline ({baseline_kill_rate:.1f}%)'
                )
        
        # Plotar a área preenchida
        plt.fill_between(df['index'], df['taxa_kill_smoothed'], color='darkblue', alpha=0.3)
        # Plotar a linha da média móvel (com marcador para destacar os commits)
        plt.plot(df['index'], df['taxa_kill_smoothed'], marker='o', linewidth=3, label=f'Média Móvel ({WINDOW_SIZE})', color='darkblue', markersize=6) 
        
        # Linha Original (para contexto da dispersão)
        plt.plot(df['index'], df['taxa_kill'], color='gray', alpha=0.2, linestyle='--', label='Original') 
        
        plt.title('Score de Mutação por Commit', fontsize=18)
        plt.xlabel('Commit', fontsize=14)
        plt.ylabel('Score de Mutação (%)', fontsize=14)
        plt.legend(fontsize=12)
        plt.grid(True, alpha=0.6, linestyle='--')
        plt.ylim(0, 105) 
        plt.tight_layout()
        plt.savefig(output_dir / '01_taxa_kill.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 01_taxa_kill.png")
        
        # Gráfico 2: Distribuição de status (Gráfico de Área Empilhada Suavizada)
        plt.figure(figsize=(14, 7))
        
        # Plotar áreas empilhadas (KILLED e SURVIVED)
        plt.stackplot(
            df['index'], 
            df['killed_smoothed'], 
            df['survived_smoothed'],
            labels=['Morto (KILLED)', 'Vivo (SURVIVED)'],
            colors=['green', 'red'],
            alpha=0.6
        )
        
        df['total_testable_smoothed'] = df['killed_smoothed'] + df['survived_smoothed']
        # Adicionar a linha de Total
        plt.plot(df['index'], df['total_testable_smoothed'], color='black', linestyle='--', linewidth=1.5, label='Total Testável')
        
        plt.title('Distribuição de Mutações KILLED vs SURVIVED', fontsize=18)
        plt.xlabel('Commit', fontsize=14)
        plt.ylabel('Quantidade de Mutações', fontsize=14)
        plt.legend(loc='upper left', fontsize=12)
        plt.grid(True, axis='y', alpha=0.6, linestyle='--')
        plt.tight_layout()
        plt.savefig(output_dir / '02_distribuicao.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 02_distribuicao.png")
        
        tempos = {
            'setup_time': [],
            'pit_time': [],
            'cleanup_time': [],
            'index': []
        }
        
        for commit in self.commits:
            time_elapsed = commit.metadata.get('time_elapsed', {})
            tempos['setup_time'].append(time_elapsed.get('setup_time', 0))
            tempos['pit_time'].append(time_elapsed.get('pit_time', 0))
            tempos['cleanup_time'].append(time_elapsed.get('cleanup_time', 0))
            tempos['index'].append(commit.index)
        
        df_time = pd.DataFrame(tempos).set_index('index')
        df_time_smoothed = df_time.rolling(window=WINDOW_SIZE, min_periods=1).mean().bfill()
        
        if tempos['index']:
            plt.figure(figsize=(12, 6))
            x = df_time_smoothed.index.values

            plt.plot(x, df_time_smoothed['setup_time'], label='Tempo de Configuração', linewidth=3, color='#4CAF50')
            plt.plot(x, df_time_smoothed['pit_time'], label='Tempo de Execução do PITest', linewidth=3, color='#2196F3')
            #plt.plot(x, df_time_smoothed['cleanup_time'], label='Tempo de Limpeza', linewidth=3, color='#FF9800')
            
            plt.title('Tempo de Execução', fontsize=16)
            plt.xlabel('Commit')
            plt.ylabel('Tempo (segundos)')
            plt.legend()
            plt.grid(True, alpha=0.5, linestyle='--')
            plt.tight_layout()
            plt.savefig(output_dir / '03_tempo_execucao.png', dpi=300)
            plt.close()
            print(f"Gráfico salvo: 03_tempo_execucao.png")
        
        # Gráfico 4: Total de mutações (Gráfico de Área Suavizada)
        plt.figure(figsize=(14, 7))

        plt.fill_between(df['index'], df['total_mutacoes_smoothed'], color='purple', alpha=0.3)
        # Plotar a linha da média móvel (com marcador)
        plt.plot(df['index'], df['total_mutacoes_smoothed'], marker='o', linewidth=3, markersize=6, color='purple', label=f'Média Móvel ({WINDOW_SIZE})')

        plt.plot(df['index'], df['total_mutacoes'], color='gray', alpha=0.2, linestyle='--', label='Original')
        
        plt.title('Total de Mutações Geradas por Commit', fontsize=18)
        plt.xlabel('Commit', fontsize=14)
        plt.ylabel('Total de Mutações', fontsize=14)
        plt.legend(fontsize=12)
        plt.grid(True, alpha=0.6, linestyle='--')
        plt.tight_layout()
        plt.savefig(output_dir / '04_total_mutacoes.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 04_total_mutacoes.png")


        if baseline_kill_rate > 0 or historical_mean_kill_rate > 0:
            plt.figure(figsize=(8, 7))
            
            labels = ['Baseline', 'Média Histórica dos Commits']
            scores = [baseline_kill_rate, historical_mean_kill_rate]
            colors = ['red', 'darkblue']
            
            # Criar o gráfico de barras
            bars = plt.bar(labels, scores, color=colors, width=0.6)
            
            # Adicionar o valor exato em cima de cada barra
            for bar in bars:
                yval = bar.get_height()
                plt.text(bar.get_x() + bar.get_width()/2, yval + 1, f'{yval:.1f}%', ha='center', va='bottom', fontsize=12, fontweight='bold')
            
            # Estética
            plt.title('Comparação do score de mutação: Média Histórica vs. Baseline', fontsize=16)
            plt.ylabel('Score de Mutação (%)', fontsize=14)
            plt.ylim(0, 105) # Limite máximo em 105%
            plt.grid(True, axis='y', alpha=0.6, linestyle='--') # Grid só no eixo Y
            plt.tight_layout()
            
            plt.savefig(output_dir / '05_comparacao_media_baseline.png', dpi=300)
            plt.close()
            print(f"Gráfico salvo: 05_comparacao_media_baseline.png")

        if baseline_time > 0 or historical_mean_time > 0:
                plt.figure(figsize=(8, 7))
                
                baseline_time_minutes = baseline_time / 60
                historical_mean_time_minutes = historical_mean_time / 60
                
                labels = ['Baseline', 'Média Histórica dos Commits']
                scores = [baseline_time_minutes, historical_mean_time_minutes]
                colors = ['red', 'blue']

                # Criar o gráfico de barras
                bars = plt.bar(labels, scores, color=colors, width=0.6)
                
                # Adicionar o valor exato em cima de cada barra
                for bar in bars:
                    yval = bar.get_height()
                    plt.text(bar.get_x() + bar.get_width()/2, yval + yval*0.02, f'{yval:.1f}m', ha='center', va='bottom', fontsize=12, fontweight='bold')
                
                # Estética
                plt.title('Comparação de Tempo de Execução: Média Histórica vs. Baseline', fontsize=16)
                plt.ylabel('Tempo de PITest (minutos)', fontsize=14)
                plt.ylim(0, max(scores)*1.15 if max(scores) > 0 else 10) # Ajusta o Y para caber o label
                plt.grid(True, axis='y', alpha=0.6, linestyle='--')
                plt.tight_layout()
                
                plt.savefig(output_dir / '06_comparacao_tempo_baseline.png', dpi=300)
                plt.close()
                print(f"Gráfico salvo: 06_comparacao_tempo_baseline.png")

@click.command()
@click.option('-p', '--path', default='.', help='Caminho do diretório de relatórios.')
@click.option('-bt', '--baseline_time', type=float, default=0.0, help='Tempo de execução (em segundos) do PITest no Baseline.')
def main(path: str, baseline_time: float):
    """Carregar e analisar relatórios PITest"""
    try:
        path_obj = Path(path).resolve()
        
        # Validar caminho
        if not path_obj.exists():
            print(f"Caminho não existe: {path_obj}")
            sys.exit(1)
        
        if not path_obj.is_dir():
            print(f"Não é um diretório: {path_obj}")
            sys.exit(1)
        
        print(f"Carregando relatórios de: {path_obj}\n")
        
        # Criar loader
        loader = ReportLoader(path_obj)

        baseline_kill_rate = loader.load_baseline_mutations() 
        
        # Carregar todos os relatórios
        if not loader.load_all_reports():
            print("Nenhum relatório encontrado")
            sys.exit(1)
        
        print(f"\n {len(loader.commits)} commits carregados\n")
        
        # Mostrar resumo
        df_summary = loader.get_all_summaries()
        print("="*70)
        print("RESUMO DOS COMMITS")
        print("="*70)
        print(df_summary[['index', 'commit', 'total_mutacoes', 'killed', 'taxa_kill']])
        
        # Salvar CSV consolidado
        csv_output = path_obj.parent / 'resumo_consolidado.csv'
        loader.save_consolidated_csv(csv_output)

        graphs_dir = path_obj.parent / 'graficos'
        loader.generate_graphs(graphs_dir, baseline_kill_rate, baseline_time)
        
        print(f"\n Análise concluída!")
        sys.exit(0)
    
    except Exception as e:
        print(f"Erro: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()