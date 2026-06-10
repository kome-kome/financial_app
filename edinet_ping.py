import asyncio, httpx, zipfile, io, pandas as pd, os
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()
API_KEY = os.environ['EDINET_API_KEY']

def _recent_weekday() -> str:
    """直近の平日（土日を除いた昨日以前の日付）を返す"""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # 5=土曜, 6=日曜
        d -= timedelta(days=1)
    return d.isoformat()

async def check():
    target_date = _recent_weekday()
    print(f"確認対象日: {target_date}")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            'https://disclosure.edinet-fsa.go.jp/api/v2/documents.json',
            params={'date': target_date, 'type': 2, 'Subscription-Key': API_KEY},
            timeout=30
        )
        results = r.json().get('results', [])
        doc = next((d for d in results if d.get('secCode') and d.get('csvFlag')=='1'), None)
        if not doc:
            print('書類なし')
            return
        print('書類:', doc['filerName'], doc['docID'])

        doc_id = doc['docID']
        r2 = await client.get(
            f'https://disclosure.edinet-fsa.go.jp/api/v2/documents/{doc_id}',
            params={'type': 5, 'Subscription-Key': API_KEY},
            timeout=60
        )
        with zipfile.ZipFile(io.BytesIO(r2.content)) as z:
            csv_files = [n for n in z.namelist() if n.endswith('.csv')]
            print('CSVファイル一覧:', csv_files)
            biggest = max(csv_files, key=lambda n: z.getinfo(n).file_size)
            print('最大CSV:', biggest)
            with z.open(biggest) as f:
                try:
                    df = pd.read_csv(f, encoding='utf-8', low_memory=False, nrows=20)
                except UnicodeDecodeError:
                    f.seek(0)
                    content = f.read().decode('utf-16')
                    df = pd.read_csv(io.StringIO(content), sep='\t', low_memory=False, nrows=20)
                print(df.head(5).to_string())

asyncio.run(check())