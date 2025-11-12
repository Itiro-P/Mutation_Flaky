#!/usr/bin/env python3

import os
import sys
import subprocess
import json
import re
import tempfile
import shutil
from pathlib import Path

class CommitMutation:
    def __init__(self, projectDir):
        self.projectDir = Path(projectDir).resolve()

        if not self.projectDir.exists():
            print(f"✗ Projeto não encontrado: {self.projectDir}")
            sys.exit(1)

        self.reportsDir = self.projectDir / "diff_analysis"
        self.reportsDir.mkdir(parents=True, exist_ok=True)
        self.currentBranch = self._getCurrentBranch()

    def _runCommand(self, cmd, cwd=None):
        """Executa comando com timeout"""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd or self.projectDir,
                capture_output=True,
                text=True,
                timeout=600
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)

    def _getCurrentBranch(self):
        """Obtém branch atual"""
        code, stdout, _ = self._runCommand("git rev-parse --abbrev-ref HEAD")
        return stdout.strip() if code == 0 else "main"

    def _getCommitDiff(self, commit):
        """Obtém diff do commit"""
        code, stdout, _ = self._runCommand(f"git show {commit}")
        return stdout if code == 0 else ""

    def _extractPackageClass(self, filePath, commit=None, diff_lines=None):
        """Extrai package e classe do arquivo.
        Tenta (na ordem):
         1) procurar 'package' nas linhas do diff (diff_lines)
         2) usar `git show {commit}:{filePath}` para ler o arquivo naquele commit (se commit informado)
         3) fallback para apenas o nome da classe (stem)
        """
        className = Path(filePath).stem

        # 1) procurar package nas linhas do diff (se fornecido)
        if diff_lines:
            for line in diff_lines:
                content = line.lstrip('+-').strip()
                m = re.search(r'^\s*package\s+([\w.]+)\s*;', content)
                if m:
                    return f"{m.group(1)}.{className}"

        # 2) tentar git show <commit>:<filePath>
        if commit:
            code, stdout, stderr = self._runCommand(f"git show {commit}:{filePath}")
            if code == 0 and stdout:
                for line in stdout.splitlines():
                    m = re.search(r'^\s*package\s+([\w.]+)\s*;', line)
                    if m:
                        return f"{m.group(1)}.{className}"

        # 3) fallback: sem package -> só classe
        return className

    def _extractMethods(self, diff_lines):
        """Extrai nomes de métodos das linhas"""
        methods = set()
        # regex simplificada, captura o nome do método (grupo 2)
        method_pattern = r'(?:public|private|protected)?\s*(?:static\s+)?(?:synchronized\s+)?(?:[\w\[\]<>]+\s+)+([a-zA-Z_][\w]*)\s*\('

        for line in diff_lines:
            content = line.lstrip('+-').strip()
            matches = re.findall(method_pattern, content)
            for method_name in matches:
                methods.add(method_name)

        return list(methods)

    def _analyzeDiff(self, diff_content, commit=None):
        """Analisa diff e extrai classes/métodos alterados.
        Recebe `commit` opcional para buscar package no commit se necessário.
        """
        changes = {}
        current_file = None
        current_lines = []
        in_java_file = False

        for line in diff_content.split('\n'):
            # Detecta novo arquivo - linha com 'diff --git a/... b/...'
            if line.startswith('diff --git'):
                # Processa arquivo anterior
                if current_file and current_lines:
                    full_class_name = self._extractPackageClass(current_file, commit=commit, diff_lines=current_lines)
                    methods = self._extractMethods(current_lines)
                    if methods:
                        changes[full_class_name] = methods

                # Novo arquivo: capturar caminho b/...
                m = re.search(r'\s+b/(.+)$', line)
                if m:
                    filename = m.group(1).strip()
                    if filename.endswith('.java') and 'src/main/java' in filename:
                        current_file = filename
                        current_lines = []
                        in_java_file = True
                    else:
                        in_java_file = False
                        current_file = None
                else:
                    in_java_file = False
                    current_file = None

            # Coleta linhas adicionadas/removidas
            elif in_java_file and current_file:
                if (line.startswith('+') or line.startswith('-')) and not line.startswith(('+++', '---')):
                    current_lines.append(line)

        # Processa último arquivo
        if current_file and current_lines:
            full_class_name = self._extractPackageClass(current_file, commit=commit, diff_lines=current_lines)
            methods = self._extractMethods(current_lines)
            if methods:
                changes[full_class_name] = methods

        return changes

    def _checkoutCommit(self, commit, use_worktree=True):
        """Faz checkout temporário.

        Se use_worktree for True, tenta criar um git worktree detached em um diretório temporário
        e retorna o caminho desse diretório em caso de sucesso. Se falhar (ou se use_worktree==False),
        cai de volta para um checkout in-place (retorna True/False).

        Retorno:
          - Se worktree foi criado com sucesso -> caminho (str) para o worktree.
          - Se fez checkout in-place com sucesso -> True.
          - Caso contrário -> False.
        """
        if use_worktree:
            tmpdir = None
            try:
                tmpdir = tempfile.mkdtemp(prefix=f"worktree_{commit}_")
                code, stdout, stderr = self._runCommand(f"git worktree add --detach {tmpdir} {commit}")
                if code == 0:
                    return str(tmpdir)
                else:
                    # cleanup e fallback
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

        # fallback: checkout in-place (menos seguro)
        code, _, _ = self._runCommand(f"git checkout {commit}")
        return True if code == 0 else False

    def _compileProject(self):
        """Compila projeto"""
        print("    Compilando...")
        code, stdout, stderr = self._runCommand("mvn clean compile test-compile")
        if code != 0:
            print("Erro na compilação:")
            print(stdout)
            print(stderr)
        return code == 0

    def _get_commit_info(self, commit):
        """Obtém info do commit"""
        msg_code, msg, _ = self._runCommand(f"git log --format=%B -n 1 {commit}")
        date_code, date, _ = self._runCommand(f"git log --format=%aI -n 1 {commit}")

        msg = msg.strip().split('\n')[0] if msg_code == 0 else "N/A"
        date = date.strip() if date_code == 0 else "N/A"

        return {"message": msg, "date": date}

    def _runPitest(self, report_dir, target_classes):
        print("    Rodando PITest...")

        if target_classes:
            classes_str = ",".join(target_classes)
            cmd = (
                f"mvn pitest:mutationCoverage "
                f"-Dpitest.targetClasses='{classes_str}' "
                f"-Dpitest.threads=4 "
                f"-Dpitest.outputFormats=HTML "
                f"-Dpitest.reportsDirectory={report_dir}"
            )
        else:
            cmd = (
                f"mvn pitest:mutationCoverage "
                f"-Dpitest.threads=4 "
                f"-Dpitest.outputFormats=HTML "
                f"-Dpitest.reportsDirectory={report_dir}"
            )

        code, stdout, stderr = self._runCommand(cmd)
        if code != 0:
            print("Erro ao rodar Pitest:")
            print(stdout)
            print(stderr)
        return (Path(report_dir) / "index.html").exists()

    def _print_summary(self, results):
        """Imprime resumo"""
        print(f"\n{'='*70}")
        print("✓ Análise Completa!")
        print(f"{'='*70}")
        print(f"Commits analisados: {len(results)}")
        print(f"Diretório: {self.reportsDir}\n")

        print("Commits processados:")
        for r in results:
            classes_count = len(r['changes'])
            print(f"  [{r['index']:02d}] {r['commit']}")
            print(f"       {r['info']['message']}")
            print(f"       Classes alteradas: {classes_count}")

        print("\nRelatórios PITest:")
        if results:
            print(f"  Primeiro: {results[0]['report_dir']}/index.html")
            print(f"  Último:   {results[-1]['report_dir']}/index.html")

        index_file = self.reportsDir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"\nÍndice salvo em: {index_file}")

    def analyzeCommits(self, num_commits):
        """Analisa últimos N commits"""
        print(f"\n{'='*70}")
        print("Análise Completa: Mudanças + PITest")
        print(f"{'='*70}")
        print(f"Projeto: {self.projectDir}")
        print(f"Commits: {num_commits}")
        print(f"Branch: {self.currentBranch}")
        print(f"{'='*70}\n")

        code, stdout, _ = self._runCommand(f"git log --oneline -n {num_commits}")

        if code != 0:
            print("✗ Erro ao obter commits")
            return False

        commits = [line.split()[0] for line in stdout.strip().split('\n') if line]
        commits = list(reversed(commits))

        print(f"Encontrados {len(commits)} commits\n")

        results = []

        for idx, commit in enumerate(commits, 1):
            print(f"{'─'*70}")
            print(f"[{idx}/{len(commits)}] Commit: {commit}")

            info = self._get_commit_info(commit)
            print(f"Mensagem: {info['message']}")
            print(f"Data: {info['date']}")

            diff = self._getCommitDiff(commit)
            changes = self._analyzeDiff(diff, commit=commit)

            if not changes:
                print("Sem mudanças em Java\n")
                continue

            print(f"Classes alteradas: {len(changes)}")

            target_classes = list(changes.keys())
            print("Classes para testar:")
            for class_name in target_classes:
                methods = changes[class_name]
                print(f"  {class_name} ({len(methods)} métodos)")

            print("\nFazendo checkout...")
            # Se você quiser evitar checkout em árvore principal, comente as próximas linhas
            if not self._checkoutCommit(commit):
                print("Erro ao fazer checkout\n")
                continue

            if not self._compileProject():
                print("Erro na compilação\n")
                # voltar para branch original e continuar
                self._runCommand(f"git checkout {self.currentBranch}")
                continue

            report_dir = self.reportsDir / f"{idx:02d}-{commit}"
            report_dir.mkdir(exist_ok=True)

            if self._runPitest(report_dir, target_classes):
                print("PITest concluído")
                print(f"Relatório: {report_dir}/index.html\n")

                result = {
                    "index": idx,
                    "commit": commit,
                    "info": info,
                    "changes": changes,
                    "report_dir": str(report_dir)
                }
                results.append(result)

                with open(report_dir / "metadata.json", 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
            else:
                print("Erro ao rodar PITest\n")

            # volta para branch original antes de processar próximo (mais seguro)
            self._runCommand(f"git checkout {self.currentBranch}")

        print(f"Voltando para branch {self.currentBranch}...")
        self._runCommand(f"git checkout {self.currentBranch}")

        self._print_summary(results)

        return True


def main():
    if len(sys.argv) < 3:
        print("Uso:")
        print("  python3 commit_mutation.py <projeto> <numero_commits>")
        print("\nExemplos:")
        print("  python3 commit_mutation.py . 10")
        print("  python3 commit_mutation.py /path/to/commons-io 20")
        sys.exit(1)

    project_dir = sys.argv[1]
    num_commits = int(sys.argv[2])

    mutation = CommitMutation(project_dir)
    success = mutation.analyzeCommits(num_commits)

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
