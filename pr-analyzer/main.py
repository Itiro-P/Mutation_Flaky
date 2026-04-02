import csv
import github3
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

GITHUB_TOKEN = ''
DESIRED_COLUMNS = ['Project URL', 'Category', 'PR Link', 'Notes']

INPUT_PATH = Path.cwd() / 'in'
OUTPUT_PATH = Path.cwd() / 'out'

def parse_url(url: str) -> dict:
    if not url: return {}
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    print(parts)
    if len(parts) < 4 or parts[2] != 'pull': return {}
    return {
        'owner': parts[0],
        'repository': parts[1],
        'pull_number': int(parts[3])
    }

def fetch_pr(line: dict, gh: github3):
    url = line.get('PR Link', '').strip()
    parsed = parse_url(url)

    if not parsed or 'owner' not in parsed:
        print(f"Pulo: URL inválida ou malformada -> {url}")
        return

    try:
        repo = gh.repository(parsed['owner'], parsed['repository'])
        if not repo:
            print(f"Pulo: Repositório não encontrado ou sem acesso -> {parsed['owner']}/{parsed['repository']}")
            return None

        pull_request = repo.pull_request(parsed['pull_number'])
        if not pull_request:
            print(f"Pulo: PR #{parsed['pull_number']} não existe em {repo.name}")
            return None

        line['commit_count'] = getattr(pull_request, 'commits_count', 0)
        line['file_count'] = pull_request.files().count if hasattr(pull_request, 'files') else 0
        line['additions_count'] = getattr(pull_request, 'additions_count', 0)
        line['deletions_count'] = getattr(pull_request, 'deletions_count', 0)
        return line



    except github3.exceptions.NotFoundError:
        print(f"Erro 404: Recurso não encontrado para {url}")
        return None
    except Exception as e:
        print(f"Erro inesperado no link {url}: {e}")
        return None

def insert_prs(filtered_csv: list) -> list:
    if not filtered_csv: return []

    gh = github3.login(token=GITHUB_TOKEN)
    if not gh:
        print("Erro: Falha na autenticação. Verifique seu GITHUB_TOKEN.")
        return filtered_csv

    csv_with_prs = []

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(fetch_pr, line, gh) for line in filtered_csv]

        for future in as_completed(futures):
            res = future.result()
            if res:
                csv_with_prs.append(res)


    return csv_with_prs

def filter_columns(data: list) -> list:
    cols = DESIRED_COLUMNS + ['commit_count', 'file_count', 'additions_count', 'deletions_count']
    return [{k: line[k] for k in cols if k in line} for line in data]

def import_input() -> list:
    if not INPUT_PATH.exists() or not INPUT_PATH.is_dir():
        print(f'Diretório {INPUT_PATH} inválido.')
        return []

    lines = []
    seen_links = set()

    for file in INPUT_PATH.glob('**/*.csv'):
        with open(file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for line in reader:
                link = line.get('PR Link', '').strip()
                if link and link not in seen_links:
                    lines.append(line)
                    seen_links.add(link)
    return lines

def export_output(data: list):
    if not data:
        print("Nenhum dado para exportar.")
        return

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)

    file_path = OUTPUT_PATH / 'resultado_prs.csv'

    keys = data[0].keys()

    try:
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=keys)

            writer.writeheader()

            writer.writerows(data)

        print(f"Arquivo salvo com sucesso em: {file_path}")
    except IOError as e:
        print(f"Erro ao salvar o arquivo: {e}")




def main():
    raw_lines = import_input()

    if not raw_lines:
        print("Nenhum dado encontrado.")
        return

    enriched_data = insert_prs(raw_lines)

    final_data = filter_columns(enriched_data)

    export_output(final_data)

    print(f"Processados {len(final_data)} registros.")

if __name__ == "__main__":
    main()
