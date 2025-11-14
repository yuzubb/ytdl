from fastapi import FastAPI, HTTPException
from yt_dlp import YoutubeDL
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.middleware.cors import CORSMiddleware
import os

# --- FastAPIインスタンス ---
app = FastAPI()

# --- CORS設定 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       
    allow_credentials=True,
    allow_methods=["*"],       
    allow_headers=["*"],       
)

# スレッドプール
# Renderの環境に合わせてワーカー数を設定
executor = ThreadPoolExecutor(max_workers=os.cpu_count() or 1)

# yt-dlp の基本オプション
ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "format": "bestvideo+bestaudio/best",
    # プロキシ設定は環境変数で管理することを推奨しますが、ここではコードを維持
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007" 
}

# キャッシュ: { video_id: (timestamp, data, duration) }
CACHE = {}
DEFAULT_CACHE_DURATION = 600    
LONG_CACHE_DURATION = 14200     

def cleanup_cache():
    """期限切れのキャッシュを削除"""
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]
    print(f"--- Cache Cleanup: Removed {len(expired)} entries. ---")

# ==============================================================================
# 既存のエンドポイント
# ==============================================================================
@app.get("/stream/{video_id}")
async def get_streams(video_id: str):
    """指定した YouTube の video_id のストリーム情報を返す"""
    current_time = time.time()
    cleanup_cache()

    # --- キャッシュチェック ---
    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            # キャッシュデータには 'formats' キーで既に整理されたフォーマットが含まれている
            return data

    url = f"https://www.youtube.com/watch?v={video_id}"

    def fetch_info():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        # スレッドで yt-dlp を実行
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
                "url": f.get("url"),
                "protocol": f.get("protocol") # m3u8フィルタリングのためにprotocolも取得
            }
            for f in info.get("formats", [])
            if f.get("url") and f.get("ext") != "mhtml"
        ]

        # --- レスポンスデータ作成 ---
        response_data = {
            "title": info.get("title"),
            "id": video_id,
            "formats": formats
        }

        # --- キャッシュ期間を決定 ---
        cache_duration = (
            LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
        )

        # --- キャッシュに保存 ---
        CACHE[video_id] = (current_time, response_data, cache_duration)

        print(f"{video_id} の {cache_duration}秒キャッシュを作成しました。URL数: {len(formats)}")

        return response_data

    except Exception as e:
        print(f"Error fetching {video_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch stream info: {str(e)}")

# ==============================================================================
# 新しい m3u8 専用エンドポイント
# ==============================================================================
@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    """指定した YouTube の video_id の m3u8 (HLS/DASHマニフェスト) ストリームを返す"""
    current_time = time.time()
    cleanup_cache()

    # --- キャッシュチェックと取得 ---
    # /stream と同じキャッシュキーを使用し、フルデータを取得する
    if video_id in CACHE:
        timestamp, data, duration = CACHE[video_id]
        if current_time - timestamp < duration:
            info_data = data
        else:
            info_data = None
    else:
        info_data = None


    # キャッシュがない場合は /stream と同じロジックで情報をフェッチし、キャッシュを更新
    if not info_data:
        url = f"https://www.youtube.com/watch?v={video_id}"
        def fetch_info():
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            loop = asyncio.get_event_loop()
            raw_info = await loop.run_in_executor(executor, fetch_info)

            # --- フォーマット整理 ---
            formats = [
                {
                    "itag": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution"),
                    "fps": f.get("fps"),
                    "acodec": f.get("acodec"),
                    "vcodec": f.get("vcodec"),
                    "url": f.get("url"),
                    "protocol": f.get("protocol")
                }
                for f in raw_info.get("formats", [])
                if f.get("url") and f.get("ext") != "mhtml"
            ]

            # フルデータをキャッシュに保存
            info_data = {
                "title": raw_info.get("title"),
                "id": video_id,
                "formats": formats
            }
            cache_duration = (
                LONG_CACHE_DURATION if len(formats) >= 12 else DEFAULT_CACHE_DURATION
            )
            CACHE[video_id] = (current_time, info_data, cache_duration)
            print(f"{video_id} の {cache_duration}秒キャッシュ(フル)を作成しました。URL数: {len(formats)}")
            
        except Exception as e:
            print(f"Error fetching {video_id} for m3u8: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch stream info: {str(e)}")


    # --- フルデータから m3u8 URLをフィルタリング ---
    m3u8_formats = [
        f for f in info_data["formats"] 
        # m3u8形式のURL、ext、またはprotocolを持つものを抽出
        if f.get("url") and (
            ".m3u8" in f["url"] 
            or f.get("ext") == "m3u8" 
            or f.get("protocol") == "m3u8_native" 
            or f.get("protocol") == "http_dash_segments" # DASHマニフェストも配信マニフェストとして含める
        )
    ]
    
    if not m3u8_formats:
        raise HTTPException(status_code=404, detail="m3u8 または DASH 形式のストリームマニフェストは見つかりませんでした。")

    # 応答データの整理 (タイトルとm3u8フォーマットのみ)
    m3u8_response = {
        "title": info_data["title"],
        "id": video_id,
        "m3u8_formats": m3u8_formats
    }
    
    return m3u8_response


# --- キャッシュ削除API (既存) ---
@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    """指定した video_id のキャッシュを削除"""
    if video_id in CACHE:
        del CACHE[video_id]
        print(f"{video_id} のキャッシュを削除しました。")
        return {"status": "success", "message": f"{video_id} のキャッシュを削除しました。"}
    else:
        raise HTTPException(status_code=404, detail="指定されたIDのキャッシュは存在しません。")

# --- キャッシュ一覧確認用 (既存) ---
@app.get("/cache")
def list_cache():
    """現在のキャッシュ一覧を返す"""
    now = time.time()
    cleanup_cache() 
    return {
        vid: {
            "age_sec": int(now - ts),
            "remaining_sec": int(dur - (now - ts)),
            "duration_sec": dur
        }
        for vid, (ts, _, dur) in CACHE.items()
    }
