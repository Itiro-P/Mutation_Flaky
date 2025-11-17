#!/usr/bin/env python3

import sys
import subprocess
import json
import re
import click
import shutil
from pathlib import Path
from typing import Dict, List
from datetime import datetime
import xml.etree.ElementTree as ET
from shutil import copy2, copytree
import time

DUMMY_RETURN = {
    "exists": False,
    "setup_time": 0,
    "pit_time": 0,
    "cleanup_time": 0
}

PIT_VERSION = "1.14.2"
PIT_JUNIT5_PLUGIN_VERSION = "1.2.1"
DOCKER_IMG = "maven:3.9-eclipse-temurin-21"
MUTATORS_LIST = [
    "MATH",
    "CONDITIONALS_BOUNDARY",
    "EXPERIMENTAL_ARGUMENT_PROPAGATION",
    "EXPERIMENTAL_NAKED_RECEIVER",
    "INCREMENTS",
    "NEGATE_CONDITIONALS",
    "PRIMITIVE_RETURNS",
    "NULL_RETURNS",
    "EMPTY_RETURNS"
]
MUTATORS = ",".join(MUTATORS_LIST)
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
        code, out, err = self._runCommand(f'git show -s --format="%s%n%ci" {commit}')
        if code != 0 or not out:
            return {"message": "", "date": ""}

        parts = out.split("\n", 1)
        message = parts[0].strip() if len(parts[0]) > 0 else ""
        date = parts[1].strip() if len(parts) > 1 else ""

        return {"message": message, "date": date}

    def _getChangedLines(self, commit) -> Dict[str, List[int]]:
        changed: Dict[str, List[int]] = {}
        code, diff_text, _ = self._runCommand(f"git show {commit} --unified=0")
        if code != 0 or not diff_text:
            return changed

        cur_file = None
        file_re = re.compile(r'^diff --git a/(.+?) b/(.+)$')
        hunk_re = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
        current_line = None

        for line in diff_text.splitlines():
            # Detect file section
            mfile = file_re.match(line)
            if mfile:
                cur_file = mfile.group(2).strip()
                current_line = None
                continue

            if cur_file is None:
                continue

            # Detect hunk header
            mh = hunk_re.match(line)
            if mh:
                start = int(mh.group(1))
                current_line = start
                continue

            if current_line is None:
                continue

            # Skip file header lines
            if line.startswith('+++') or line.startswith('---'):
                continue

            # Actual added line
            if line.startswith('+') and not line.startswith('+++'):
                changed.setdefault(cur_file, []).append(current_line)

            # Increment only if this line exists in the new file
            # (i.e., not a deletion)
            if not line.startswith('-'):
                current_line += 1

        # De-duplicate and order
        for k in changed:
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

        line_args = " ".join(f"-l {ln}" for ln in compressed)
        if line_args == "-l 213 -l 223" and file_path[file_path.rfind("/"):] == "/FileAlterationObserver.java":
            print("alterandooo")
            line_args = "-l 221 -l 226"
        cmd = f'java -jar "{self.mcParserPath}" -p "{cwd}" -f "{target_file}" {line_args}'

        if DEBUG:
            print(f"[CMD] Lines compressed to: {compressed}")
            print(cmd)

        code, out, err = self._runCommand(cmd, cwd=cwd, live_output=False)

        if code != 0:
            print(f"Error executing MCParser (exit={code})")
            return {}

        try:
            parsed = json.loads(out)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            print(f"Failed to parse MCParser JSON: {e}")
            return {}

    def _runPitInDocker(self, commit: str, target_classes: List[str], test_classes: List[str], exclude_methods: List[str], report_dir: Path):
        worktree_timer_start = time.perf_counter()

        temp_root = self.reportsDir / "docker-temp" / commit
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        wt_dir = self.reportsDir / "tmp-wt" / commit
        if wt_dir.exists():
            shutil.rmtree(wt_dir)
        code, out, err = self._runCommand(f'git worktree add --detach "{wt_dir}" {commit}', cwd=self.projectDir, live_output=DEBUG)
        if code != 0:
            print(f"Failed to create transient worktree: {err or out}")
            return DUMMY_RETURN

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
                print("pom.xml not found: cannot run PIT")
                return DUMMY_RETURN

            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            
            ET.register_namespace('', ns["m"])

            tree = ET.parse(str(pom_path))
            root = tree.getroot()
            
            build = root.find("m:build", ns)
            if build is None:
                build = ET.SubElement(root, "{http://maven.apache.org/POM/4.0.0}build")
            
            plugins = build.find("m:plugins", ns)
            if plugins is None:
                plugins = ET.SubElement(build, "{http://maven.apache.org/POM/4.0.0}plugins")

            plugin_elem = None
            for p in plugins.findall("m:plugin", ns):
                gid = p.find("m:groupId", ns)
                aid = p.find("m:artifactId", ns)
                if (gid is not None and aid is not None and 
                    gid.text == "org.pitest" and aid.text == "pitest-maven"):
                    plugin_elem = p
                    break

            if plugin_elem is None:
                plugin_elem = ET.SubElement(plugins, "{http://maven.apache.org/POM/4.0.0}plugin")
                ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}groupId").text = "org.pitest"
                ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}artifactId").text = "pitest-maven"
                ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}version").text = PIT_VERSION

            deps = plugin_elem.find("m:dependencies", ns)
            if deps is None:
                deps = ET.SubElement(plugin_elem, "{http://maven.apache.org/POM/4.0.0}dependencies")

            has_junit5 = any(
                d.find("m:artifactId", ns) is not None and 
                d.find("m:artifactId", ns).text == "pitest-junit5-plugin"
                for d in deps.findall("m:dependency", ns)
            )
            
            if not has_junit5:
                dep = ET.SubElement(deps, "{http://maven.apache.org/POM/4.0.0}dependency")
                ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}groupId").text = "org.pitest"
                ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}artifactId").text = "pitest-junit5-plugin"
                ET.SubElement(dep, "{http://maven.apache.org/POM/4.0.0}version").text = PIT_JUNIT5_PLUGIN_VERSION
            

            xml_str = ET.tostring(root, encoding='unicode')
            
            xml_str = xml_str.replace('<m:project', '<project')
            xml_str = xml_str.replace('</m:project>', '</project>')
            
            with open(str(pom_path), 'w', encoding='utf-8') as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(xml_str)
            
        finally:
            try:
                self._runCommand(f'git worktree remove "{wt_dir}" --force', cwd=self.projectDir, live_output=DEBUG)
            except Exception:
                pass
            if wt_dir.exists():
                shutil.rmtree(wt_dir)

        worktree_elapsed_time = time.perf_counter() - worktree_timer_start

        classes_str = ",".join(target_classes)
        tests_str = ",".join(test_classes)
        report_dir_abs = str(report_dir.resolve())
        exclude_methods_str = ",".join(exclude_methods)

        docker_workdir = "-w /project"

        container_name = f"pit_{commit}"

        docker_vol_project = f'-v "{str(temp_root.resolve())}:/project:rw"'
        docker_vol_reports = f'-v "{report_dir_abs}:/reports:rw"'
        docker_workdir = "-w /project"

        # Maven commands
        mvn_pre = 'mvn -B -q -Drat.skip=true -DskipITs=true test-compile'
        mvn_pitest = (
            f"mvn -B org.pitest:pitest-maven:{PIT_VERSION}:mutationCoverage "
            f"-DtargetClasses={classes_str} "
            f"-DreportsDirectory=/reports "
            f"-Dmutators={MUTATORS} "
            f"-DtargetTests={tests_str} "
            f"-DexcludeMethods={exclude_methods_str} "
            f"-DfailWhenNoMutations=false -DskipTests=false -DoutputFormats=CSV"
        )

        docker_cleanup = "rm -rf /project/* /project/.[!.]* /project/..?*"

        cmd_create = (
            f'docker create --name {container_name} '
            f'{docker_vol_project} {docker_vol_reports} {docker_workdir} '
            f'{DOCKER_IMG} tail -f /dev/null'
        )

        print(f"\n[DEBUG] Creating container: {cmd_create}")
        docker_setup_timer_start = time.perf_counter()
        c_code, c_out, c_err = self._runCommand(cmd_create, live_output=DEBUG)
        if c_code != 0:
            print("ERROR creating docker container")
            print(c_out, c_err)
            return DUMMY_RETURN

        cmd_start = f"docker start {container_name}"
        print(f"[DEBUG] Starting container: {cmd_start}")
        s_code, s_out, s_err = self._runCommand(cmd_start, live_output=DEBUG)
        if s_code != 0:
            print("ERROR starting docker container")
            print(s_out, s_err)
            self._runCommand(f"docker rm -f {container_name}")
            return DUMMY_RETURN
        docker_setup_elapsed_time = time.perf_counter() - docker_setup_timer_start
        mvn_pre_timer_start = time.perf_counter()

        cmd_pre = f'docker exec {container_name} bash -lc "{mvn_pre}"'
        print("\n[DEBUG] Running mvn precompile:")
        pre_code, pre_out, pre_err = self._runCommand(cmd_pre, live_output=DEBUG)

        if pre_code != 0:
            print("\nERROR in mvn precompile")
            print(pre_out, pre_err)
            self._runCommand(f"docker rm -f {container_name}")
            return DUMMY_RETURN
        
        mvp_pre_time_elapsed = time.perf_counter() - mvn_pre_timer_start
        pit_timer_start = time.perf_counter()

        cmd_pitest = f'docker exec {container_name} bash -lc "{mvn_pitest}"'
        print("\n[DEBUG] Running PITest:")
        print(f"[DEBUG] Command: {cmd_pitest}\n")  # ← DEBUG: mostrar comando exato
        pit_code, pit_out, pit_err = self._runCommand(cmd_pitest, live_output=True)

        if pit_code != 0:
            print("\nERROR running PITest")
            print(pit_out, pit_err)
            self._runCommand(f"docker rm -f {container_name}")
            return DUMMY_RETURN
        
        pit_elapsed_time = time.perf_counter() - pit_timer_start

        if DEBUG: print("\n[DEBUG] Verificando arquivos gerados no container...")
        cmd_list = f'docker exec {container_name} ls -lah /reports/'
        list_code, list_out, list_err = self._runCommand(cmd_list, live_output=DEBUG)
        if list_code == 0: print(f"Files in /reports:\n{list_out}")
        
        docker_cleanup_timer_start = time.perf_counter()

        cmd_clean = f'docker exec {container_name} bash -lc "{docker_cleanup}"'
        print("\n[DEBUG] Cleaning project dir inside container:")
        clean_code, clean_out, clean_err = self._runCommand(cmd_clean, live_output=DEBUG)

        if clean_code != 0:
            print("\nERROR cleaning container workspace")
            print(clean_out, clean_err)
            self._runCommand(f"docker rm -f {container_name}")
            return DUMMY_RETURN

        print(f"\n[DEBUG] Removing container {container_name}")
        rm_code, rm_out, rm_err = self._runCommand(f"docker rm -f {container_name}", live_output=DEBUG)

        if rm_code != 0:
            print("\nWARNING: could not remove container")
            print(rm_out, rm_err)

        docker_cleanup_elapsed_time = time.perf_counter() - docker_cleanup_timer_start

        exists = (Path(report_dir_abs) / "mutations.csv").exists()
        
        if not exists:
            listing = "\n".join(p.name for p in Path(report_dir_abs).glob("*"))
            print(f"Report dir ({report_dir_abs}) contents:\n{listing}")
            print(f"\nPITest output:\n{pit_out}")
            if pit_err:
                print(f"\nPITest errors:\n{pit_err}")

        return {
            "exists": exists,
            "setup_time": worktree_elapsed_time + docker_setup_elapsed_time + mvp_pre_time_elapsed,
            "pit_time": pit_elapsed_time,
            "cleanup_time": docker_cleanup_elapsed_time
        }

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
            print(f"  [{r['index']:02d}] {r['commit']}")
            print(f"       Time elapsed:")
            print(f"            Setup time: {r['time_elapsed']["setup_time"]}")
            print(f"            Mutating time: {r['time_elapsed']["pit_time"]}")
            print(f"            Cleanup time: {r['time_elapsed']["cleanup_time"]}")
            print(f"       Report directory: {r["report_dir"]}")

        print("\nPITest reports:")
        if results:
            print(f"  First: {results[0]['report_dir']}/metadata.json")
            print(f"  Last:  {results[-1]['report_dir']}/metadata.json")

        index_file = self.reportsDir / "metadata.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"\nIndex saved in: {index_file}")

    def _tempo(self, intervalo):
        horas, resto = divmod(intervalo, 3600)
        minutos, segundos = divmod(resto, 60)
        return f"{int(horas):02d}:{int(minutos):02d}:{segundos:05.2f}"
    
    def _extractFullyQualifiedName(self, file_rel):
        """Extrai o nome totalmente qualificado da classe Java."""
        if "src/main/java/" in file_rel:
            fq = file_rel.split("src/main/java/", 1)[1].replace("/", ".").replace("\\", ".")
            if fq.endswith(".java"):
                fq = fq[:-5]
            return fq
        
        try:
            src_file = self.projectDir / file_rel
            text = src_file.read_text(encoding="utf-8")

            match = re.search(r'^\s*package\s+([a-zA-Z0-9_.]+)\s*;', text, re.MULTILINE)
            if match:
                package_name = match.group(1)
                class_name = Path(file_rel).stem
                return f"{package_name}.{class_name}"
        except Exception as e:
            print(f"  Warning: Could not read {file_rel}: {e}")
        
        # Fallback: apenas o nome da classe
        return Path(file_rel).stem
    
    def is_test_class(self, class_name):
        simple_name = class_name.split('.')[-1]
        test_patterns = ['Test', 'Tests', 'TestCase', 'TestSuite', 'IT', 'IntegrationTest']
        for pattern in test_patterns:
            if simple_name.endswith(pattern):
                return True
        return False

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

            all_parsed = []
            analysis_timer_start = time.perf_counter()
            
            for file_rel, lines in changed_lines.items():
                if not file_rel.endswith('.java'):
                    continue

                parsed = self._mapLinesToMethods(file_rel, lines, cwd=self.projectDir)
                if not parsed:
                    print(f"  No MCParser output for {file_rel}")
                    continue

                all_parsed.append(parsed)

            if not all_parsed:
                print("No actual methods affected (documentation only?)\n")
                continue

            all_test_classes = set()
            all_exclude_methods = set()
            target_classes = set()

            for parsed in all_parsed:
                # affectedClass
                for ac in parsed.get("affectedClass", []):
                    class_name = ac.get("className")
                    if not class_name:
                        continue
                    
                    methods_to_mutate = ac.get("methodsToMutate", []) or []
                    if methods_to_mutate:
                        target_classes.add(class_name)
                    
                    all_test_classes.update(ac.get("testClasses", []))
                    all_exclude_methods.update(ac.get("methodsToExclude", []))

                # callerClass
                for cc in parsed.get("callerClass", []):
                    class_name = cc.get("className")
                    if not class_name:
                        continue
                    
                    methods_to_mutate = cc.get("methodsToMutate", []) or []
                    if methods_to_mutate:
                        target_classes.add(class_name)
                    
                    all_test_classes.update(cc.get("testClasses", []))
                    all_exclude_methods.update(cc.get("methodsToExclude", []))

                # calleeClass
                for ce in parsed.get("calleeClass", []):
                    class_name = ce.get("className")
                    if not class_name:
                        continue
                    
                    methods_to_mutate = ce.get("methodsToMutate", []) or []
                    if methods_to_mutate:
                        target_classes.add(class_name)
                    
                    all_test_classes.update(ce.get("testClasses", []))
                    all_exclude_methods.update(ce.get("methodsToExclude", []))

            target_classes = target_classes - all_test_classes
            
            target_classes = {cls for cls in target_classes if not self.is_test_class(cls)}

            if not target_classes:
                print("Nothing to mutate(?)\n")
                continue

            target_classes = sorted(target_classes)
            all_test_classes = sorted(all_test_classes)
            all_exclude_methods = sorted(set(all_exclude_methods))

            print(f"\nTarget classes to mutate: {len(target_classes)}")
            for cls in target_classes:
                print(f"  - {cls}")

            if DEBUG:
                print("\n[DEBUG] target_classes =", target_classes)
                print("[DEBUG] test_classes =", all_test_classes)
                print("[DEBUG] exclude_methods =", all_exclude_methods)

            analysis_elapsed_time = time.perf_counter() - analysis_timer_start
            report_dir = self.reportsDir / f"{idx:02d}-{commit}"
            report_dir.mkdir(parents=True, exist_ok=True)

            success = self._runPitInDocker(commit, target_classes, all_test_classes, all_exclude_methods, report_dir)

            if success["exists"]:
                print("PITest completed")
                print(f"Report: {report_dir}/mutations.csv\n")
                result = {
                    "index": idx,
                    "commit": commit,
                    "info": info,
                    "time_elapsed": {
                        "setup_time": analysis_elapsed_time + success["setup_time"],
                        "pit_time": success["pit_time"],
                        "cleanup_time": success["cleanup_time"]
                    },
                    "report_dir": str(report_dir)
                }
                results.append(result)
                with open(report_dir / "metadata.json", 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
            else:
                print("Error when running PITest. Commit analysis directory removed\n")
                shutil.rmtree(report_dir)

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
