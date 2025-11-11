#!/usr/bin/env python3

import os
import sys
import subprocess
import json
import re
from pathlib import Path
from datetime import datetime

class DiffAnalyzer:
    def __init__(self, projectDir):
        self.projectDir = Path(projectDir).resolve()
        self.reportsDir = self.projectDir / "diff_analysis"
        self.reportsDir.mkdir()

    def _runCommand(self, cmd, cwd=None):
        try:
            result = subprocess.run(cmd, True, cwd or self.projectDir, True, True, 60)
            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return 1, "", str(e)

    def _getCommitDiff(self, commit):
        code, stdout, stderr = self._runCommand(f"git show {commit}")
        return stdout if code == 0 else ""

    def _extractPackageClass(self, filePath):
        try:
            with open(self.projectDir / filePath, 'r') as f:
                content = f.readlines(15)
                match = None
                className = Path(filePath).stem
                for line in content:
                    result = re.search(r'package\s+([\w.]+)\s*;', line)
                    if result is not None:
                        match = result.group(1)
                        break
                if match is not None:
                    return f"{match}.{className}"
                return className

        except Exception as e:
            print("Exceção lançada: ", e)
            return Path(filePath).stem

    def _extractMethods(self, diff_lines):
        methods = set()
        method_pattern = r'(public|private|protected)?\s+(?:static)?\s+(?:synchronized)?\s+(?:[\w\[\]<>]+\s+)+(\w+)\s*\('

        for line in diff_lines:
            # Remove + ou - do início
            content = line.lstrip('+-').strip()

            # Procura por assinatura de método
            matches = re.findall(method_pattern, content)
            for match in matches:
                method_name = match[1]
                methods.add(method_name)
        return list(methods)

    def _analyzeDiff(self, diff_content):
        changes = {}
        current_file = None
        current_lines = []
        in_java_file = False

        for line in diff_content.split('\n'):
            # Detecta novo arquivo
            if line.startswith('diff --git'):
                # Processa arquivo anterior
                if current_file and current_lines:
                    full_class_name = self._extractPackageClass(current_file)
                    methods = self._extractMethods(current_lines)
                    if methods:
                        changes[full_class_name] = methods

                    # Novo arquivo
                    match = re.search(r'b/(.+?)$', line)
                    if match:
                        filename = match.group(1)
                        if filename.endswith('.java') and 'src/main/java' in filename:
                            current_file = filename
                            current_lines = []
                            in_java_file = True
                        else:
                            in_java_file = False
                            current_file = None

                # Coleta linhas adicionadas/removidas
                elif in_java_file and current_file:
                    if line.startswith(('+', '-')) and not line.startswith(('+++', '---')):
                        current_lines.append(line)

            # Processa último arquivo
            if current_file and current_lines:
                full_class_name = self._extractPackageClass(current_file)
                methods = self._extractMethods(current_lines)
                if methods:
                    changes[full_class_name] = methods

            return changes

    def analyzeCommit(self, commit):
        # Info do commit
        msgCode, msg, _ = self._runCommand(f"git log --format=%B -n 1 {commit}")
        msg = msg.strip().splitlines()[0] if msgCode == 0 else "N/A"

        # Obtém diff
        diff = self._getCommitDiff(commit)

        if not diff:
            print("✗ Erro ao obter diff")
            return None

        # Analisa
        changes = self._analyzeDiff(diff)

        if not changes:
            print("Sem mudanças em Java")
            return None

        # Mostra resultado
        print(f"Classes alteradas: {len(changes)}\n")

        for class_name in sorted(changes.keys()):
            methods = changes[class_name]
            print(f"  {class_name}")
            for method in sorted(methods):
                print(f"    - {method}()")

        return changes

def main():
    if len(sys.argv) < 2:
        print("Uso:")
        print("  python3 commit_mutation.py <projeto> <numero de commits>")
        print("\nExemplos:")
        print("  python3 commit_mutation.py . 10")
        print("  python3 commit_mutations.py . 20")
        sys.exit(1)

    project_dir = sys.argv[1]
    analyzer = DiffAnalyzer(project_dir)
    return

if __name__ == "__main__":
    main()
