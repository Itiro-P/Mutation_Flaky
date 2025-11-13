#!/usr/bin/env python3

import sys
import subprocess
import json
import re
import click
from pathlib import Path
from typing import Dict, List, Tuple

PIT_VER = "1.14.2"
MAX_THREADS = 4
DEBUG = True

class CommitAnalyzer:
    """
        Class that analyses commits incrementally using mutation testing in:
        - Altered lines.
            - Methods that also *calls* these lines (**callers**);
            - Methods *called* by these lines (**callees**).
        
        The mutation score calculated in each commit is a measure to ensure software quality.
    """

    def __init__(self, projectDir, count):
        self.projectDir = Path(projectDir).resolve()
        self.repoName = self._getRepositoryName()
        self.timestamp = self._getTimestamp()
        
        # Create organized directory structure
        # diff_analysis/common-collections/2025-11-12_21-30-45/
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
        """
        Extract repository name from project path.
        Example: /home/user/projects/common-collections -> common-collections
        """
        return self.projectDir.name

    def _getTimestamp(self):
        """
        Get current timestamp in format YYYY-MM-DD_HH-MM-SS
        """
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def _runCommand(self, cmd: str, cwd: Path = None, live_output: bool = False, timeout: int = 600):
            """
                Run a command using the shell.
                You can also force `stdout` to show using `live_output = True`
            """
            cwd = cwd or self.projectDir
            try:
                proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            except Exception as e:
                return 1, "", str(e)

            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []

            try:
                if live_output:
                    # stream stdout while collecting stderr separately
                    assert proc.stdout is not None
                    assert proc.stderr is not None
                    while True:
                        line = proc.stdout.readline()
                        if line:
                            print(line, end="", flush=True)
                            stdout_chunks.append(line)
                        else:
                            # check if finished
                            if proc.poll() is not None:
                                break
                    # read any remaining stdout
                    rest = proc.stdout.read()
                    if rest:
                        stdout_chunks.append(rest)
                    # read stderr fully
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
        """
            Retrieves the current branch. Used internally.
        """
        code, stdout, stderr = self._runCommand("git rev-parse --abbrev-ref HEAD")
        return stdout.strip() if code == 0 else "main"

    def _getCommitDiff(self, commit):
        """
            Retrieves the diff's commit. Used internally.
        """
        code, stdout, stderr = self._runCommand(f"git show {commit} --unified=0")
        return stdout if code == 0 else ""

    def _getChangedLines(self, commit) -> Dict[str, List[int]]:
        """
            Extract altered lines on a commit using diffs.\n
            Uses git show --unified=0 to get a hunk with no context, parses @@ -a,b +c,d @@
        """
        changed: Dict[str, List[int]] = {}
        code, diff_text, _ = self._runCommand(f"git show {commit} --unified=0")
        if code != 0 or not diff_text:
            return changed

        cur_file = None
        # pattern for file marker: diff --git a/path b/path
        file_re = re.compile(r'^diff --git a/(.+?) b/(.+)$')
        hunk_re = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')

        for line in diff_text.splitlines():
            mfile = file_re.match(line)
            if mfile:
                # take new path
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

        # dedupe and sort
        for k in list(changed.keys()):
            changed[k] = sorted(set(changed[k]))
        return changed

    def _mapLinesToMethods(self, file_path: str, lines: List[int], cwd: Path = None):
        """
        Calls MCParser to retrieve changed methods as well as callers and callees.
        Compresses consecutive line numbers into ranges (e.g., 20-25).
        """
        if not lines:
            return {}

        sorted_lines = sorted(set(lines))
        
        # Compress consecutive lines into ranges
        compressed = []
        i = 0
        while i < len(sorted_lines):
            start = sorted_lines[i]
            end = start
            
            # Find consecutive sequence
            while i + 1 < len(sorted_lines) and sorted_lines[i + 1] == sorted_lines[i] + 1:
                i += 1
                end = sorted_lines[i]
            
            # Add range or single number
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

        # Build command with multiple -l arguments (preserving ranges)
        lines_str = " ".join(f"-l {ln}" for ln in compressed)
        cmd = f"java -jar \"{self.mcParserPath}\" -f \"{target_file}\" {lines_str}"
        
        print(f"[CMD] Lines compressed to: {compressed}")  # DEBUG
        
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

    def _compileProject(self, cwd=None, live_output=DEBUG):
        """
            Compiles project in the current working directory (worktree)
        """
        print(f"    Compiling... (cwd={cwd or self.projectDir})")
        code, out, err = self._runCommand("mvn clean compile test-compile -DskipTests=true", cwd=cwd, live_output=DEBUG)
        if code != 0:
            print("Erro na compilação:")
            if out:
                print(out)
            if err:
                print(err)
        return code == 0

    def _runPitest(self, report_dir, target_classes: List[str], cwd=None, live_output=DEBUG, mutators: str = None):
        """
            Runs PITest. Used internally
        """
        print(f"    Running PITest... (cwd={cwd or self.projectDir})")
        if target_classes:
            classes_str = ",".join(target_classes)
            base = f"mvn org.pitest:pitest-maven:{PIT_VER}:mutationCoverage -Dpitest.targetClasses='{classes_str}' -Dpitest.threads={MAX_THREADS} -Dpitest.outputFormats=HTML -Dpitest.reportsDirectory={report_dir}"
        else:
            base = f"mvn org.pitest:pitest-maven:{PIT_VER}:mutationCoverage -Dpitest.threads={MAX_THREADS} -Dpitest.outputFormats=HTML -Dpitest.reportsDirectory={report_dir}"

        if mutators:
            base += f" -Dpitest.mutators={mutators}"

        code, out, err = self._runCommand(base, cwd=cwd, live_output=DEBUG)
        if code != 0:
            print("Error when running PITest:")
            if err:
                print(err)

        return code == 0

    def _printResults(self, results):
        """
        Prints the results of the analysis.
        """
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

        print("\nPITest reports:")
        if results:
            print(f"  First: {results[0]['report_dir']}/index.html")
            print(f"  Last:  {results[-1]['report_dir']}/index.html")

        index_file = self.reportsDir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"\nIndex saved in: {index_file}")

    def _getCommitInfo(self, commit):
        """
            Gets the commit info. Used internally.
        """
        msg_code, msg, _ = self._runCommand(f"git log --format=%B -n 1 {commit}")
        date_code, date, _ = self._runCommand(f"git log --format=%aI -n 1 {commit}")
        msg = msg.strip().split('\n')[0] if msg_code == 0 else "N/A"
        date = date.strip() if date_code == 0 else "N/A"
        return {"message": msg, "date": date}

    def analyze(self):
        """
        Runs the incremental mutation analysis.
        """
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
                
                # Call MCParser to map lines to methods
                mapped = self._mapLinesToMethods(file_rel, lines, cwd=self.projectDir)
                
                # DEBUG: Print what MCParser returned
                if DEBUG:
                    print(f"\n[DEBUG] MCParser output for {file_rel}:")
                    print(f"  Affected: {mapped.get('affectedMethods', [])}")
                    print(f"  Callers:  {mapped.get('callers', [])}")
                    print(f"  Callees:  {mapped.get('callees', [])}")
                
                # Skip if no affected methods found (Javadoc/comments only)
                if not mapped.get("affectedMethods"):
                    print(f"  No methods affected (Javadoc/comments?)")
                    continue
                
                cls = Path(file_rel).stem
                changes[cls] = {
                    "affected": mapped.get("affectedMethods", []),
                    "callers": mapped.get("callers", []),
                    "callees": mapped.get("callees", [])
                }
                print(f"  {len(mapped['affectedMethods'])} methods found")
                print(f"  {len(mapped.get('callers', []))} callers found")
                print(f"  {len(mapped.get('callees', []))} callees found")

            if not changes:
                print("No actual methods affected (documentation only)\n")
                continue

            print(f"\nAltered classes: {len(changes)}")
            target_classes = list(changes.keys())

            print("Classes to test:")
            for class_name, methods in changes.items():
                affected_count = len(methods['affected'])
                callers_count = len(methods['callers'])
                callees_count = len(methods['callees'])
                print(f"  {class_name}:")
                print(f"    - Affected: {affected_count}")
                print(f"    - Callers:  {callers_count}")
                print(f"    - Callees:  {callees_count}")

            # Checkout commit
            code, _, _ = self._runCommand(f"git checkout {commit}")
            if code != 0:
                print("Error checking out commit\n")
                continue

            # Compile project
            if not self._compileProject(cwd=self.projectDir, live_output=DEBUG):
                print("Error when compiling.\n")
                self._runCommand(f"git checkout {self.currentBranch}")
                continue

            # Run PITest
            report_dir = self.reportsDir / f"{idx:02d}-{commit}"
            report_dir.mkdir(parents=True, exist_ok=True)

            success = self._runPitest(str(report_dir), target_classes, cwd=self.projectDir, live_output=DEBUG)
            
            if success:
                print("PITest completed")
                print(f"Report: {report_dir}/index.html\n")
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
                
                # DEBUG: Print what was saved to metadata
                print(f"[DEBUG] Metadata saved with {len(changes)} classes")
                for cls, data in changes.items():
                    print(f"  {cls}: {len(data['affected'])} affected, {len(data['callers'])} callers, {len(data['callees'])} callees")
            else:
                print("Error when running PITest.\n")

            # Restore branch
            self._runCommand(f"git checkout {self.currentBranch}")

        print(f"\nRestoring branch {self.currentBranch}...")
        self._runCommand(f"git checkout {self.currentBranch}")

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