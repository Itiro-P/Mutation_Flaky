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
            
            # Verificar se diretórios de commit estão diretamente aqui
            # Estrutura: reports_path/01-{hash}/, reports_path/03-{hash}/, etc
            
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
    
    def generate_graphs(self, output_dir: Path):
        """Gerar gráficos"""
        if not self.commits:
            print("Nenhum commit para gerar gráficos")
            return
        
        df = self.get_all_summaries()
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Gráfico 1: Taxa de kill
        plt.figure(figsize=(12, 6))
        plt.plot(df['index'], df['taxa_kill'], marker='o', linewidth=2)
        plt.xlabel('Commit')
        plt.ylabel('Taxa de Kill (%)')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(output_dir / '01_taxa_kill.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 01_taxa_kill.png")
        
        # Gráfico 2: Distribuição de status
        plt.figure(figsize=(12, 6))
        plt.plot(df['index'], df['killed'], marker='o', label='Killed', linewidth=2, markersize=8)
        plt.plot(df['index'], df['survived'], marker='s', label='Survived', linewidth=2, markersize=8)
        #plt.plot(df['index'], df['no_coverage'], marker='^', label='No Coverage', linewidth=2, markersize=8)
        plt.xlabel('Commit')
        plt.ylabel('Quantidade de Mutações')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(output_dir / '02_distribuicao.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 02_distribuicao.png")
        
        # Gráfico 3: Tempo de execução (setup, pit, cleanup)
        # Extrair tempos da metadata
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
        
        if tempos['index']:  # Se tem dados de tempo
            plt.figure(figsize=(12, 6))
            x = tempos['index']
            plt.plot(x, tempos['setup_time'], marker='o', label='Setup Time', linewidth=2, markersize=8)
            plt.plot(x, tempos['pit_time'], marker='s', label='PITest Time', linewidth=2, markersize=8)
            #plt.plot(x, tempos['cleanup_time'], marker='^', label='Cleanup Time', linewidth=2, markersize=8)
            plt.xlabel('Commit')
            plt.ylabel('Tempo (segundos)')
            plt.legend()
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(output_dir / '03_tempo_execucao.png', dpi=300)
            plt.close()
            print(f"Gráfico salvo: 03_tempo_execucao.png")
        
        # Gráfico 4: Total de mutações
        plt.figure(figsize=(12, 6))
        plt.plot(df['index'], df['total_mutacoes'], marker='o', linewidth=2, markersize=8, color='purple')
        plt.xlabel('Commit')
        plt.ylabel('Total de Mutações')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(output_dir / '04_total_mutacoes.png', dpi=300)
        plt.close()
        print(f"Gráfico salvo: 04_total_mutacoes.png")

@click.command()
@click.option('-p', '--path', default='.', help='Caminho do diretório de relatórios.')
def main(path: str):
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
        
        # Carregar todos os relatórios
        if not loader.load_all_reports():
            print("Nenhum relatório encontrado")
            sys.exit(1)
        
        print(f"\n✓ {len(loader.commits)} commits carregados\n")
        
        # Mostrar resumo
        df_summary = loader.get_all_summaries()
        print("="*70)
        print("RESUMO DOS COMMITS")
        print("="*70)
        print(df_summary[['index', 'commit', 'total_mutacoes', 'killed', 'taxa_kill']])
        
        # Salvar CSV consolidado
        csv_output = path_obj.parent / 'resumo_consolidado.csv'
        loader.save_consolidated_csv(csv_output)
        
        # Gerar gráficos
        graphs_dir = path_obj.parent / 'graficos'
        loader.generate_graphs(graphs_dir)
        
        print(f"\n✓ Análise concluída!")
        sys.exit(0)
    
    except Exception as e:
        print(f"Erro: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()