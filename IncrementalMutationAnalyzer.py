#!/usr/bin/env python3

import sys
import subprocess
import json
import re
import click
import shutil
import os
from pathlib import Path
from typing import Dict, List
from datetime import datetime
import xml.etree.ElementTree as ET
from shutil import copy2, copytree
import time

uid = os.getuid() if hasattr(os, "getuid") else 1000
gid = os.getgid() if hasattr(os, "getgid") else 1000

PIT_VERSION = "1.14.2"
PIT_JUNIT5_PLUGIN_VERSION = "1.2.1"
DOCKER_IMG = "maven:3.9-eclipse-temurin-21"
MUTATORS = [
    "MATH", 
    "CONDITIONALS_BOUNDARY", 
    "EXPERIMENTAL_ARGUMENT_PROPAGATION", 
    "EXPERIMENTAL_NAKED_RECEIVER", 
    "VOID_METHOD_CALLS", 
    "NON_VOID_METHOD_CALLS", 
    "INCREMENTS", 
    "NEGATE_CONDITIONALS"
]
PIT_CONFIG = None
DEBUG = True

class CommitAnalyzer:
    """
        Analyzes commits incrementally using mutation testing in Docker containers.
    """

    def __init__(self, projectDir, count):
        self.projectDir = Path(projectDir).resolve()
        self.repoName = self._getRepositoryName()
        self.timestamp = self._getTimestamp()
        self.reportsDir = Path.cwd() / "diff_analysis" / self.repoName / self.timestamp
        self.mcParserPath = Path(Path.cwd() / "MCParser" / "target" / "MCParser-1.0.jar").resolve()
        self.count = count
        self.currentBranch = self._getCurrentBranch()

        if not self.projectDir.exists():
            print(f"Path given does not correspond to a git repository: {self.projectDir}")
            sys.exit(1)

        if not self.mcParserPath.exists():
            print(f"MCParser not found in {self.mcParserPath}")
            sys.exit(1)

        self.reportsDir.mkdir(parents=True, exist_ok=True)

    def _getRepositoryName(self):
        return self.projectDir.name

    def _getTimestamp(self):
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def _runCommand(self, cmd: str, cwd: Path = None, live_output: bool = False, timeout: int = 600):
        cwd = cwd or self.projectDir
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as e:
            return 1, "", str(e)

        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []

        try:
            if live_output:
                assert proc.stdout is not None
                assert proc.stderr is not None
                while True:
                    line = proc.stdout.readline()
                    if line:
                        print(line, end="", flush=True)
                        stdout_chunks.append(line)
                    else:
                        if proc.poll() is not None:
                            break
                rest = proc.stdout.read()
                if rest:
                    stdout_chunks.append(rest)
                stderr_chunks.append(proc.stderr.read() or "")
            else:
                out, err = proc.communicate(timeout=timeout)
                stdout_chunks.append(out or "")
                stderr_chunks.append(err or "")

            ret = proc.wait(timeout=1)
            return ret, "".join(stdout_chunks), "".join(stderr_chunks)
        except subprocess.TimeoutExpired:
            proc.kill()
            return 124, "".join(stdout_chunks), "Timeout"
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return 1, "".join(stdout_chunks), str(e)

    def _getCurrentBranch(self):
        code, stdout, stderr = self._runCommand("git rev-parse --abbrev-ref HEAD")
        return stdout.strip() if code == 0 else "main"

    def _getCommitInfo(self, commit):
        msg_code, msg, _ = self._runCommand(f"git log --format=%B -n 1 {commit}")
        date_code, date, _ = self._runCommand(f"git log --format=%aI -n 1 {commit}")
        msg = msg.strip().split('\n')[0] if msg_code == 0 else "N/A"
        date = date.strip() if date_code == 0 else "N/A"
        return {"message": msg, "date": date}

    def _getChangedLines(self, commit) -> Dict[str, List[int]]:
        changed: Dict[str, List[int]] = {}
        code, diff_text, _ = self._runCommand(f"git show {commit} --unified=0")
        if code != 0 or not diff_text:
            return changed

        cur_file = None
        file_re = re.compile(r'^diff --git a/(.+?) b/(.+)$')
        hunk_re = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')

        for line in diff_text.splitlines():
            mfile = file_re.match(line)
            if mfile:
                cur_file = mfile.group(2).strip()
                continue

            if cur_file is None:
                continue

            mh = hunk_re.match(line)
            if mh:
                start = int(mh.group(1))
                count = int(mh.group(2)) if mh.group(2) else 1
                if count <= 0:
                    continue
                lst = changed.setdefault(cur_file, [])
                for i in range(start, start + count):
                    lst.append(i)
                continue

        for k in list(changed.keys()):
            changed[k] = sorted(set(changed[k]))
        return changed

    def _mapLinesToMethods(self, file_path: str, lines: List[int], cwd: Path = None):
        if not lines:
            return {}

        sorted_lines = sorted(set(lines))
        compressed = []
        i = 0
        while i < len(sorted_lines):
            start = sorted_lines[i]
            end = start
            while i + 1 < len(sorted_lines) and sorted_lines[i + 1] == sorted_lines[i] + 1:
                i += 1
                end = sorted_lines[i]
            if start == end:
                compressed.append(str(start))
            else:
                compressed.append(f"{start}-{end}")
            i += 1

        target_file = (cwd / file_path) if cwd and not Path(file_path).is_absolute() else Path(file_path)
        if not target_file.exists():
            alt = self.projectDir / file_path
            if alt.exists():
                target_file = alt
            else:
                print(f"File not found: {file_path}")
                return {}

        lines_str = " ".join(f"-l {ln}" for ln in compressed)
        cmd = f"java -jar \"{self.mcParserPath}\" -f \"{target_file}\" {lines_str}"
        
        if DEBUG:
            print(f"[CMD] Lines compressed to: {compressed}")
        
        code, out, err = self._runCommand(cmd, cwd=cwd, live_output=DEBUG)
        
        if code != 0:
            print(f"Error executing MCParser (exit={code})")
            return {}

        try:
            parsed = json.loads(out)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            print(f"Failed to parse MCParser JSON: {e}")
            return {}

    def _runPitInDocker(self, commit: str, target_classes: List[str], report_dir: Path) -> bool:
        temp_root = self.reportsDir / "docker-temp" / commit
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        try:
            wt_dir = self.reportsDir / "tmp-wt" / commit
            if wt_dir.exists():
                shutil.rmtree(wt_dir)
            code, out, err = self._runCommand(f'git worktree add --detach "{wt_dir}" {commit}', cwd=self.projectDir, live_output=DEBUG)
            if code != 0:
                print(f"Failed to create transient worktree: {err or out}")
                return False

            try:
                # copy minimal files
                for name in ["pom.xml", "src", "mvnw", ".mvn"]:
                    src = wt_dir / name
                    dest = temp_root / name
                    if src.exists():
                        if src.is_dir():
                            try:
                                copytree(src, dest, dirs_exist_ok=True)
                            except TypeError:
                                shutil.copytree(src, dest)
                        else:
                            copy2(src, dest)

                pom_path = temp_root / "pom.xml"
                if not pom_path.exists():
                    print("pom.xml not found -> cannot run PIT")
                    return False

                # inject pitest plugin + junit5 plugin
                ns = {"m": "http://maven.apache.org/POM/4.0.0"}
                ET.register_namespace('', ns["m"])
                tree = ET.parse(str(pom_path))
                root = tree.getroot()
                build = root.find("m:build", ns) or ET.SubElement(root, "{http://maven.apache.org/POM/4.0.0}build")
                plugins = build.find("m:plugins", ns) or ET.SubElement(build, "{http://maven.apache.org/POM/4.0.0}plugins")
                plugin_elem = None
                for p in plugins.findall("m:plugin", ns):
                    gid = p.find("m:groupId", ns)
                    aid = p.find("m:artifactId", ns)
                    if gid is not None and aid is not None and gid.text == "org.pitest" and aid.text == "pitest-maven":
                        plugin_elem = p
                        break
                if plugin_elem is None:
                    plugin_elem = ET.SubElement(plugins, "{http://maven.apache.org/POM/4.0.0}plugin")
                    ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}groupId").text = "org.pitest"
                    ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}artifactId").text = "pitest-maven"
                    ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}version").text = PIT_VERSION
                deps = plugin_elem.find("m:dependencies", ns) or ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}dependencies")
                if not any(d.find("m:artifactId", ns).text == "pitest-junit5-plugin" for d in deps.findall("m:dependency", ns)):
                    dep = ET.SubElement(deps, "{http://maven.apache.org/POM/4.0.0}dependency")
                    ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}groupId").text = "org.pitest"
                    ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}artifactId").text = "pitest-junit5-plugin"
                    ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}version").text = PIT_JUNIT5_PLUGIN_VERSION
                tree.write(str(pom_path), encoding="utf-8", xml_declaration=True)

            finally:
                try:
                    self._runCommand(f'git worktree remove "{wt_dir}" --force', cwd=self.projectDir, live_output=DEBUG)
                except Exception:
                    pass
                if wt_dir.exists():
                    shutil.rmtree(wt_dir)

            classes_str = ",".join(target_classes)
            tests_str = ",".join(".".join(fq.split(".")[:-1]) + ".*Test" if "." in fq else fq + "*Test" for fq in target_classes)
            report_dir_abs = str(report_dir.resolve())

            try:
                host_uid = os.getuid()
                host_gid = os.getgid()
            except Exception:
                host_uid = host_gid = 1000

            mvn_pre = 'mvn -B -q -Drat.skip=true -DskipITs=true test-compile'
            mvn_pitest = (
                f'mvn -B org.pitest:pitest-maven:{PIT_VERSION}:mutationCoverage '
                f'-DtargetClasses="{classes_str}" -DtargetTests="{tests_str}" '
                f'-DreportsDirectory="/reports" '
                f'{f"-Dmutators={",".join(MUTATORS)} " if len(MUTATORS) > 0 else ""}'
                f'-DfailWhenNoMutations=false -DskipTests=false'
            )

            docker_cmd = (
                'docker run --rm '
                '-v "%s:/project:rw" -v "%s:/reports:rw" -w /project '
                'maven:3.9-eclipse-temurin-21 bash -lc "%s && %s && chown -R %d:%d /reports"'
                % (str(temp_root.resolve()), report_dir_abs, mvn_pre, mvn_pitest, host_uid, host_gid)
            )

            if DEBUG: print("[DEBUG] Docker command:", docker_cmd)

            code, out, err = self._runCommand(docker_cmd, cwd=temp_root, live_output=DEBUG, timeout=3600)
            if code != 0:
                print("Error when running PITest:")
                if out: print("=== DOCKER STDOUT ===\n", out)
                if err: print("=== DOCKER STDERR ===\n", err)
                return False

            exists = (Path(report_dir_abs) / "index.html").exists()
            if not exists:
                try:
                    listing = "\n".join(p.name for p in Path(report_dir_abs).glob("*"))
                    print(f"Report dir contents: {listing}")
                except Exception:
                    pass

            return exists

        finally:
            try:
                if temp_root.exists():
                    shutil.rmtree(temp_root)
            except Exception as e:
                print(f"Warning cleaning temp dir: {e}")

    def _printResults(self, results):
        print(f"\n{'='*70}")
        print("Analysis completed")
        print(f"{'='*70}")
        print(f"Analysed commits: {len(results)}")
        print(f"Repository: {self.repoName}")
        print(f"Timestamp: {self.timestamp}")
        print(f"Reports directory: {self.reportsDir}\n")

        print("Processed commits:")
        for r in results:
            classes_count = len(r['changes'])
            print(f"  [{r['index']:02d}] {r['commit']}")
            print(f"       {r['info']['message']}")
            print(f"       Altered classes: {classes_count}")
            print(f"       Time elapsed: {r['time_elapsed']}")

        print("\nPITest reports:")
        if results:
            print(f"  First: {results[0]['report_dir']}/index.html")
            print(f"  Last:  {results[-1]['report_dir']}/index.html")

        index_file = self.reportsDir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"\nIndex saved in: {index_file}")

    def _tempo(self, intervalo):
        horas, resto = divmod(intervalo, 3600)
        minutos, segundos = divmod(resto, 60)
        return f"{int(horas):02d}:{int(minutos):02d}:{segundos:05.2f}"

    def analyze(self):
        print(f"\n{'='*70}")
        print("Incremental commit mutation analysis")
        print(f"{'='*70}")
        print(f"Project: {self.projectDir}")
        print(f"Commits: {self.count}")
        print(f"Branch: {self.currentBranch}")
        print(f"{'='*70}\n")

        code, stdout, _ = self._runCommand(f"git log --oneline -n {self.count}")
        if code != 0:
            print("Error retrieving commits.")
            return False

        commits = [line.split()[0] for line in stdout.strip().split('\n') if line]
        commits = list(reversed(commits))
        print(f"Found {len(commits)} commits.\n")

        results = []

        for idx, commit in enumerate(commits, 1):
            print(f"{'─'*70}")
            print(f"[{idx}/{len(commits)}] Commit: {commit}")

            info = self._getCommitInfo(commit)
            print(f"Message: {info['message']}")
            print(f"Date: {info['date']}")

            changed_lines = self._getChangedLines(commit)
            if not changed_lines:
                print("No altered lines detected\n")
                continue

            changes = {}
            for file_rel, lines in changed_lines.items():
                if not file_rel.endswith('.java') or 'src/main/java' not in file_rel:
                    continue
                
                mapped = self._mapLinesToMethods(file_rel, lines, cwd=self.projectDir)
                
                if not mapped.get("affectedMethods"):
                    print(f"  No methods affected (Javadoc/comments?)")
                    continue
                
                rel_path = file_rel
                if "src/main/java/" in rel_path:
                    fq = rel_path.split("src/main/java/", 1)[1].replace("/", ".").replace("\\", ".")
                    if fq.endswith(".java"):
                        fq = fq[:-5]
                else:
                    try:
                        src_file = (self.projectDir / file_rel)
                        text = src_file.read_text(encoding="utf-8")
                        m = re.search(r'^\s*package\s+([a-zA-Z0-9_.]+)\s*;', text, re.MULTILINE)
                        if m:
                            fq = m.group(1) + "." + Path(file_rel).stem
                        else:
                            fq = Path(file_rel).stem
                    except Exception:
                        fq = Path(file_rel).stem

                short_name = Path(file_rel).stem
                changes[fq] = {
                    "affected": mapped.get("affectedMethods", []),
                    "callers": mapped.get("callers", []),
                    "callees": mapped.get("callees", []),
                    "_short": short_name
                }

            if not changes:
                print("No actual methods affected (documentation only)\n")
                continue

            print(f"\nAltered classes: {len(changes)}")
            target_classes = list(changes.keys())

            print("Classes to test:")
            for class_name, methods in changes.items():
                print(f"  {class_name}:")
                print(f"    - Affected: {len(methods['affected'])}")
                print(f"    - Callers:  {len(methods['callers'])}")
                print(f"    - Callees:  {len(methods['callees'])}")

            report_dir = self.reportsDir / f"{idx:02d}-{commit}"
            report_dir.mkdir(parents=True, exist_ok=True)
            
            mutStart = time.perf_counter()
            success = self._runPitInDocker(commit, target_classes, report_dir)
            mutEnd = time.perf_counter()
            
            if success:
                print("PITest completed")
                print(f"Report: {report_dir}/index.html\n")
                result = {
                    "index": idx,
                    "commit": commit,
                    "info": info,
                    "changes": changes,
                    "time_elapsed": self._tempo(mutEnd - mutStart),
                    "report_dir": str(report_dir)
                }
                results.append(result)
                with open(report_dir / "metadata.json", 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
            else:
                print("Error when running PITest.\n")

        self._printResults(results)
        return True

@click.command()
@click.option('-p', '--path', default='.', help='Path of a git repository.')
@click.option('-c', '--count', default=10, help='Number of commits to analyze.')
def main(path, count):
    """Incremental mutation analyzer for git commits"""
    ca = CommitAnalyzer(path, count)
    result = ca.analyze()
    sys.exit(0 if result else 1)

if __name__ == "__main__":
    main()
