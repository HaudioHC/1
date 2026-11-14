# download_creator_images.py
import os
import json
import sys
import requests
import argparse
import time
import zipfile
import io
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# --- 全局变量 ---
API_BASE_URL = "https://civitai.com/api/v1/images"
download_count = 0
total_images = 0
progress_lock = Lock()

# 扫描轮数与下载重试
SCAN_PASSES = 3
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 2  # 秒

def setup_arguments():
    """设置所有命令行参数"""
    parser = argparse.ArgumentParser(
        description="A powerful multi-threaded image downloader for Civitai creators.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    # --- 核心设置 ---
    parser.add_argument("--username", type=str, required=True, help="The username of the Civitai creator.")
    parser.add_argument("--output-dir", type=str, default="./downloads", help="Directory to save the final zip file.")
    
    # --- API过滤与排序 ---
    parser.add_argument("--nsfw", type=str, default="X", choices=["All", "None", "Soft", "Mature", "X"],
                        help="Filter by NSFW level.\n'X' (default): X-rated images.\n'All': Gets every image.")
    parser.add_argument("--sort", type=str, default="Newest", choices=["Most Reactions", "Most Comments", "Newest"],
                        help="Sorting order for the images.")
    parser.add_argument("--period", type=str, default="AllTime", choices=["AllTime", "Year", "Month", "Week", "Day"],
                        help="Time period for sorting.")
    
    # --- 性能与格式化 ---
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 201), metavar="[1-200]",
                        help="Number of images to fetch per API call (default: 100).\nAPI allows between 1 and 200.")
    parser.add_argument("--threads", type=int, default=10, metavar="[1-32]", choices=range(1, 33),
                        help="Number of concurrent download threads (default: 10).")
    parser.add_argument("--jpeg-quality", type=int, default=92, metavar="[1-95]", choices=range(1, 96),
                        help="Quality for JPEG conversion (1-95, default: 92).")
    parser.add_argument("--scan-passes", type=int, default=SCAN_PASSES, metavar="[1-10]", choices=range(1, 11),
                        help=f"How many times to rescan the image list and take union (default: {SCAN_PASSES}).")
    parser.add_argument("--download-retries", type=int, default=DOWNLOAD_RETRIES, metavar="[0-10]", choices=range(0, 11),
                        help=f"Retries per image download on failure (default: {DOWNLOAD_RETRIES}).")
    parser.add_argument("--no-zip", action='store_true', help="Do not create a zip archive. Keep individual JPEG files.")

    return parser.parse_args()

FETCH_RETRIES = 4        # 列表页请求失败(429/5xx/超时)时的总尝试次数
FETCH_BACKOFF = 2        # 指数退避基数(秒); 429 有 Retry-After 就按它等


def fetch_json(url, params=None, timeout=20, attempts=FETCH_RETRIES):
    """带重试的 GET。返回 (data, error)。

    单张图有 --download-retries, 但列表页之前是「失败一次就 break」, 于是偶发的 429/503
    会让整轮扫描提前截断, 这个用户的图片数就少了。这里按状态码退避重试, 429 优先用
    Retry-After 里的秒数。
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.exceptions.HTTPError(f'HTTP {response.status_code}', response=response)
            response.raise_for_status()
            return response.json(), None
        except Exception as e:
            last = e
            if attempt >= attempts:
                break
            retry_after = None
            resp = getattr(e, 'response', None)
            if resp is not None:
                try:
                    retry_after = float(resp.headers.get('Retry-After'))
                except (TypeError, ValueError):
                    retry_after = None
            wait = retry_after if retry_after else FETCH_BACKOFF * attempt
            print(f"  ↻ 请求失败(第 {attempt}/{attempts} 次), {wait:.0f}s 后重试: {e}")
            time.sleep(wait)
    return None, last


def fetch_image_metadata_once(params, pass_index):
    """单次遍历所有页面，获取图片元数据。"""
    print(f"  --- 第 {pass_index} 轮扫描 ---")
    images_by_id = {}
    next_url = API_BASE_URL
    is_first_page = True

    while next_url:
        data, error = fetch_json(next_url, params=params if is_first_page else None)
        if error is not None:
            # 重试耗尽: 保留本轮已收集的部分, 由后面的扫描轮次补漏
            print(f"  ✗ API请求失败(已重试 {FETCH_RETRIES} 次): {error}")
            break

        is_first_page = False

        items = data.get('items', [])
        if not items:
            break

        for item in items:
            img_id = item.get('id')
            if img_id is not None:
                images_by_id[img_id] = item

        print(f"  > 本轮已收集 {len(images_by_id)} 张图片...")

        next_url = data.get('metadata', {}).get('nextPage')

    print(f"  [*] 第 {pass_index} 轮扫描结束，本轮 {len(images_by_id)} 张。")
    return images_by_id

def fetch_all_image_metadata(params, scan_passes=SCAN_PASSES):
    """
    第一阶段：多轮扫描列表页，对结果取并集，降低漏扫概率。
    """
    print(f"[1/3] 正在获取所有图片信息（共扫描 {scan_passes} 轮，取并集）...")
    merged = {}

    for i in range(1, scan_passes + 1):
        pass_map = fetch_image_metadata_once(params, i)
        before = len(merged)
        merged.update(pass_map)
        added = len(merged) - before
        print(f"  [*] 并集更新: 新增 {added} 张，当前总计 {len(merged)} 张。")
        if i < scan_passes:
            time.sleep(1)

    all_images = list(merged.values())
    print(f"[*] 多轮扫描完成，并集共 {len(all_images)} 张图片。\n")
    return all_images

def process_and_download_image(image_info, output_path, jpeg_quality, max_retries=DOWNLOAD_RETRIES):
    """
    下载、转换并保存单张图片。失败时自动重试。
    """
    global download_count
    
    image_id = image_info.get('id')
    image_url = image_info.get('url')
    username = image_info.get('username', 'unknown')

    if not image_id or not image_url:
        return f"信息不完整，跳过: {image_info}"

    last_error = None
    for attempt in range(1, max_retries + 2):  # 1 次首次 + max_retries 次重试
        try:
            response = requests.get(image_url, timeout=30)
            response.raise_for_status()
            
            image_bytes = io.BytesIO(response.content)
            img = Image.open(image_bytes)
            
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            jpeg_filename = f"{username}_{image_id}.jpeg"
            jpeg_filepath = os.path.join(output_path, jpeg_filename)
            img.save(jpeg_filepath, 'jpeg', quality=jpeg_quality)

            with progress_lock:
                download_count += 1
                print(f"  [{download_count}/{total_images}] ✓ 下载并转换为 {jpeg_filename}")
            
            return None
        except Exception as e:
            last_error = e
            if attempt <= max_retries:
                # 429 优先按 Retry-After 等, 否则按次数递增退避
                wait = DOWNLOAD_RETRY_DELAY * attempt
                resp = getattr(e, 'response', None)
                if resp is not None and resp.status_code == 429:
                    try:
                        wait = float(resp.headers.get('Retry-After'))
                    except (TypeError, ValueError):
                        wait = DOWNLOAD_RETRY_DELAY * attempt * 2
                print(f"  ↻ 图片ID {image_id} 第 {attempt} 次失败，{wait:.0f}s 后重试: {e}")
                time.sleep(wait)
            else:
                break

    return f"  [{download_count}/{total_images}] ✗ 处理图片ID {image_id} 失败（已重试 {max_retries} 次）: {last_error}"

def create_zip_archive(source_dir, zip_filepath):
    """将目录中的所有JPEG文件压缩成一个zip文件。"""
    print("\n[3/3] 正在创建 ZIP 压缩包...")
    try:
        with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(source_dir):
                for file in files:
                    if file.endswith('.jpeg'):
                        filepath = os.path.join(root, file)
                        zf.write(filepath, arcname=file)
        
        print(f"[*] 成功创建压缩包: {zip_filepath}")
        
        print("[*] 正在清理临时文件...")
        for file in os.listdir(source_dir):
            if file.endswith('.jpeg'):
                os.remove(os.path.join(source_dir, file))
        os.rmdir(source_dir)
        
    except Exception as e:
        print(f"  ✗ 创建ZIP或清理文件时出错: {e}")


def main():
    """主执行函数"""
    global total_images
    args = setup_arguments()
    
    temp_image_dir = os.path.join(args.output_dir, args.username)
    os.makedirs(temp_image_dir, exist_ok=True)
    
    api_params = {"username": args.username, "limit": args.limit, "sort": args.sort, "period": args.period}
    if args.nsfw != "All":
        api_params["nsfw"] = args.nsfw

    # 1. 多轮扫描并取并集
    all_image_data = fetch_all_image_metadata(api_params, scan_passes=args.scan_passes)
    total_images = len(all_image_data)
    if total_images == 0:
        print("[*] 没有找到任何图片，程序退出。")
        return 2        # 2 = 该用户名下确实没有图片(不是故障, 批量脚本不会再重试)

    # 2. 使用线程池进行下载和处理（带重试）
    print(f"[2/3] 开始使用 {args.threads} 个线程进行下载和转换（失败重试 {args.download_retries} 次）...")
    failures = []
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = [
            executor.submit(
                process_and_download_image,
                img_data,
                temp_image_dir,
                args.jpeg_quality,
                args.download_retries,
            )
            for img_data in all_image_data
        ]

        for future in as_completed(futures):
            result = future.result()
            if result:
                print(result)
                failures.append(result.strip())

    print("\n[*] 所有图片处理完成。")
    print(f"[*] 成功 {download_count}/{total_images} 张，失败 {len(failures)} 张。")

    # 失败清单落盘（写在 output 根目录，不会被打进分卷 zip），便于判断重跑时补哪几张
    if failures:
        report_path = os.path.join(args.output_dir, f"_{args.username}_failed.json")
        try:
            with open(report_path, 'w', encoding='utf8') as f:
                json.dump({"username": args.username, "total": total_images,
                           "success": download_count, "failures": failures},
                          f, ensure_ascii=False, indent=2)
            print(f"[*] 失败清单已写入 {report_path}，重跑本用户即可补齐缺图。")
        except Exception as e:
            print(f"  ✗ 写入失败清单出错: {e}")

    # 一张都没下成就返回非 0，让批量脚本判定为「需要重试」
    if download_count == 0:
        print("\n[FAIL] 一张图片都没能下载成功。")
        return 1

    # 3. 创建ZIP压缩包
    if not args.no_zip:
        zip_filename = f"civitai_{args.username}_images.zip"
        zip_filepath = os.path.join(args.output_dir, zip_filename)
        create_zip_archive(temp_image_dir, zip_filepath)
    else:
        print("[*] 已跳过创建ZIP压缩包。JPEG文件保存在 " + temp_image_dir)

    print("\n[SUCCESS] 所有任务已完成！")
    return 0

if __name__ == "__main__":
    sys.exit(main())
