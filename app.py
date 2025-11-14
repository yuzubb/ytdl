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
    # 最高画質と最高音声を組み合わせる形式で情報を取得
    "format": "bestvideo+bestaudio/best", 
    # プロキシ設定は環境変数で管理することを推奨しますが、ここではコードを維持
    "proxy": "http://ytproxy-siawaseok.duckdns.org:3007" 
}

# キャッシュ: { video_id: (timestamp, data, duration) }
CACHE = {}
DEFAULT_CACHE_DURATION = 600    # 10分
LONG_CACHE_DURATION = 14200     # 約4時間

def cleanup_cache():
    """期限切れのキャッシュを削除"""
    now = time.time()
    expired = [vid for vid, (ts, _, dur) in CACHE.items() if now - ts >= dur]
    for vid in expired:
        del CACHE[vid]
    print(f"--- Cache Cleanup: Removed {len(expired)} entries. ---")


# ==============================================================================
# エンドポイント 1: /stream/{video_id} (全フォーマット)
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
                "protocol": f.get("protocol"),
                "vbr": f.get("vbr"),
                "abr": f.get("abr"),
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
# エンドポイント 2: /m3u8/{video_id} (HLS/DASHマニフェスト)
# ==============================================================================
@app.get("/m3u8/{video_id}")
async def get_m3u8_streams(video_id: str):
    """指定した YouTube の video_id の m3u8 (HLS/DASHマニフェスト) ストリームを返す"""
    
    # 既存の /stream のロジックを呼び出して、キャッシュを利用しつつフルデータを取得
    try:
        info_data = await get_streams(video_id)
    except HTTPException as e:
        raise e

    # --- フルデータから m3u8 URLをフィルタリング ---
    m3u8_formats = [
        f for f in info_data["formats"] 
        # m3u8形式のURL、ext、またはprotocolを持つものを抽出
        if f.get("url") and (
            ".m3u8" in f["url"] 
            or f.get("ext") == "m3u8" 
            or f.get("protocol") in ["m3u8_native", "http_dash_segments"]
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


# ==============================================================================
# エンドポイント 3: /high/{video_id} (最高画質ストリームURL - webm優先)
# ==============================================================================
@app.get("/high/{video_id}")
async def get_high_quality_stream(video_id: str):
    """指定した YouTube の video_id の最高画質ストリームURL (webm優先) を返す"""
    
    # 既存の /stream のロジックを呼び出して、キャッシュを利用しつつフルデータを取得
    try:
        info_data = await get_streams(video_id)
    except HTTPException as e:
        raise e

    # --- フィルタリングロジック ---
    best_format = None
    formats = info_data["formats"]

    # 1. 統合ストリーム (動画+音声) の抽出
    combined_formats = [
        f for f in formats 
        if f.get("acodec") not in ["none", None] and f.get("vcodec") not in ["none", None]
    ]

    # 2. 統合ストリームの中で最高品質 (vbr/abrが高い) かつ webm 形式を優先して選択
    if combined_formats:
        # vbr (動画ビットレート) を基準に降順でソート
        sorted_combined = sorted(combined_formats, key=lambda x: x.get("vbr") or 0, reverse=True)
        
        # webm 形式を探す
        webm_format = next((f for f in sorted_combined if f.get("ext") == "webm"), None)
        
        if webm_format:
            best_format = webm_format
        else:
            # webm がなければ、統合ストリームの中で最高画質のものを選ぶ
            best_format = sorted_combined[0]


    # 3. 統合ストリームが見つからなかった場合のフォールバック (最高画質の動画ストリーム)
    if not best_format:
        separated_formats = [
            f for f in formats 
            if (f.get("acodec") in ["none", None] or f.get("vcodec") in ["none", None]) and f.get("url")
        ]
        
        # 最高の動画ストリーム (vcodecがあり、acodecがない)
        best_video = next(
            (
                f for f in sorted(separated_formats, key=lambda x: x.get("vbr") or 0, reverse=True)
                if f.get("vcodec") not in ["none", None] and f.get("acodec") in ["none", None]
            ), 
            None
        )
        
        if best_video:
            best_format = best_video
            best_format['note'] = 'NOTE: This is a separate video stream (no audio) because no combined stream was found.'
        else:
             # 動画ストリームも見つからない場合、最高音質をフォールバックとして返すことも可能だが、
             # ユーザー要求は「最高画質」なのでここではエラーとする
             raise HTTPException(status_code=404, detail="最高画質のストリームURLが見つかりませんでした。")

    # 4. 応答データの整理
    high_response = {
        "title": info_data["title"],
        "id": video_id,
        "format": best_format
    }
    
    return high_response


# ==============================================================================
# キャッシュ管理エンドポイント
# ==============================================================================

# --- キャッシュ削除API ---
@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    """指定した video_id のキャッシュを削除"""
    if video_id in CACHE:
        del CACHE[video_id]
        print(f"{video_id} のキャッシュを削除しました。")
        return {"status": "success", "message": f"{video_id} のキャッシュを削除しました。"}
    else:
        raise HTTPException(status_code=404, detail="指定されたIDのキャッシュは存在しません。")

# --- キャッシュ一覧確認用 ---
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
