from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os

# --- FastAPIインスタンス ---
# Vercelでは、このファイル名（index.py）と変数名（app）がデフォルトのエントリポイントとして認識されます。
app = FastAPI()

# --- CORS設定 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # 全オリジン許可
    allow_credentials=True,
    allow_methods=["*"],       # GET, POST, DELETEなど全て許可
    allow_headers=["*"],       # カスタムヘッダーも許可
)

# スレッドプール（yt-dlp は同期的なのでスレッドで動かす）
# Vercel環境でリソースを適切に利用するため、最大スレッド数を調整することが推奨されます。
executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 1)

# yt-dlp の基本オプション
# プロキシ設定は環境変数などを使って外部から設定する方がベターですが、ここでは元のコードを維持します。
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    # 注意: このプロキシURLはVercel環境からアクセス可能である必要があります。
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007" 
}

# キャッシュ: { video_id: (timestamp, data, duration) }
CACHE = {}
DEFAULT_CACHE_DURATION = 600    # 通常: 10分
LONG_CACHE_DURATION = 14200     # URL数が多い場合: 4時間 (約4時間)

def cleanup_cache():
    """期限切れのキャッシュを削除"""
    now = time.time()
    # 注: 辞書変更中に反復処理を行うのを避けるため、削除するキーのリストを作成
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]
    
    # ログはVercelのコンソールに出力されます
    print(f"--- Cache Cleanup: Removed {len(expired)} entries. ---")

@app.get("/streams/{video_id}")
async def get_streams(video_id: str):
    """指定した YouTube の video_id のストリーム情報を返す"""
    current_time = time.time()
    cleanup_cache()  # 毎回古いキャッシュを整理

    # --- キャッシュチェック ---
    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            # print(f"キャッシュヒット: {video_id}")
            return data  # キャッシュから即返す

    url = f"https://www.youtube.com/watch?v={video_id}"

    # yt-dlp 実行部分を関数化
    def fetch_info():
        # YoutubeDLオブジェクトはスレッドセーフではないため、呼び出しごとにインスタンス化します
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        # スレッドで yt-dlp を実行（非同期に待機）
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(executor, fetch_info)

        # --- フォーマット整理 ---
        formats = [
            {
                "itag": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution"),
                "fps": f.get("fps"),
                "acodec": f.get("acodec"),
                "vcodec": f.get("vcodec"),
                "url": f.get("url")
            }
            for f in info.get("formats", [])
            # f.get("url")があり、かつmhtml形式（ダウンロードリンクではない可能性が高い）を除外
            if f.get("url") and f.get("ext") != "mhtml"
        ]

        # --- レスポンスデータ作成 ---
        response_data = {
            "title": info.get("title"),
            "id": video_id,
            "formats": formats
        }

        # --- キャッシュ期間を決定 ---
        # 取得されたフォーマットの数が多い場合は、再取得コストが高いとみなし、長めにキャッシュ
        cache_duration = (
            LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        )

        # --- キャッシュに保存 ---
        CACHE[video_id] = (current_time, response_data, cache_duration)

        # --- ログ出力 ---
        print(f"{video_id} の {cache_duration}秒キャッシュを作成しました。URL数: {len(formats)}")

        return response_data

    except Exception as e:
        # エラーはVercelのログに出力
        print(f"Error fetching {video_id}: {e}")
        # クライアントには一般的なエラーを返す
        raise HTTPException(status_code=500, detail=f"Failed to fetch stream info: {str(e)}")

# --- キャッシュ削除API ---
@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    """指定した video_id のキャッシュを削除"""
    if video_id in CACHE:
        del CACHE[video_id]
        print(f"{video_id} のキャッシュを削除しました。")
        return {"status": "success", "message": f"{video_id} のキャッシュを削除しました。"}
    else:
        # print(f"Cache not found for {video_id}")
        raise HTTPException(status_code=404, detail="指定されたIDのキャッシュは存在しません。")

# --- キャッシュ一覧確認用 ---
@app.get("/cache")
def list_cache():
    """現在のキャッシュ一覧を返す"""
    now = time.time()
    # 期限切れをチェックしてから返す方が親切ですが、ここでは単純に一覧を返します
    cleanup_cache() 
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
