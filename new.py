# download_creator_images.py
import os
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
    # <-- 修改：移除 'All'，默认改为 'None'
    parser.add_argument("--nsfw", type=str, default="None", choices=["None", "Soft", "Mature", "X"],
                        help="Filter by NSFW level. 'None' (default): SFW only.")
    parser.add_argument("--sort", type=str, default="Newest", choices=["Most Reactions", "Most Comments", "Newest"],
                        help="Sorting order for the images.")
    parser.add_argument("--period", type=str, default="AllTime", choices=["AllTime", "Year", "Month", "Week", "Day"],
                        help="Time period for sorting.")
    
    # --- 性能与格式化 (新功能) ---
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 201), metavar="[1-200]",
                        help="Number of images to fetch per API call (default: 100).\nAPI allows between 1 and 200.")
    # <-- 新增：自定义下载数量
    parser.add_argument("--image-count", type=int, default=0, metavar="N",
                        help="Limit the number of images to download (0 for all, default: 0).\nDownloads the first N images based on the sort order.")
    parser.add_argument("--threads", type=int, default=10, metavar="[1-32]", choices=range(1, 33),
                        help="Number of concurrent download threads (default: 10).")
    parser.add_argument("--jpeg-quality", type=int, default=85, metavar="[1-95]", choices=range(1, 96),
                        help="Quality for JPEG conversion (1-95, default: 85).")
    parser.add_argument("--no-zip", action='store_true', help="Do not create a zip archive. Keep individual JPEG files.")

    return parser.parse_args()

def fetch_all_image_metadata(params):
    """
    第一阶段：遍历所有页面，获取所有图片的元数据（URL、ID等）。
    这是为了预先知道总图片数，以便显示进度。
    """
    print("[1/3] 正在获取所有图片信息...")
    all_images = []
    next_url = API_BASE_URL
    is_first_page = True

    while next_url:
        try:
            response = requests.get(next_url, params=params if is_first_page else None, timeout=20)
            response.raise_for_status()
            data = response.json()
            is_first_page = False
            
            items = data.get('items', [])
            if not items:
                break
            
            all_images.extend(items)
            print(f"  > 已找到 {len(all_images)} 张图片...")
            
            next_url = data.get('metadata', {}).get('nextPage')
        except requests.exceptions.RequestException as e:
            print(f"  ✗ API请求失败: {e}")
            break
            
    print(f"[*] 总共找到 {len(all_images)} 张图片。\n")
    return all_images

def process_and_download_image(image_info, output_path, jpeg_quality):
    """
    下载、转换并保存单张图片。此函数将在多线程中执行。
    """
    global download_count
    
    image_id = image_info.get('id')
    image_url = image_info.get('url')
    username = image_info.get('username', 'unknown')

    if not image_id or not image_url:
        return f"信息不完整，跳过: {image_info}"

    try:
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        image_bytes = io.BytesIO(response.content)
        img = Image.open(image_bytes)
        
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        
        jpeg_filename = f"{username}_{image_id}.jpeg"
        jpeg_filepath = os.path.join(output_path, jpeg_filename)
        img.save(jpeg_filepath, 'jpeg', quality=jpeg_quality)

        with progress_lock:
            download_count += 1
            print(f"  [{download_count}/{total_images}] ✓ 下载并转换为 {jpeg_filename}")
        
        return None # 表示成功
    except Exception as e:
        return f"  [{download_count}/{total_images}] ✗ 处理图片ID {image_id} 失败: {e}"

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
    
    # 准备API参数
    # <-- 修改：现在总是包含 nsfw 参数，因为 'All' 选项已移除
    api_params = {
        "username": args.username,
        "limit": args.limit,
        "sort": args.sort,
        "period": args.period,
        "nsfw": args.nsfw
    }

    # 1. 获取所有图片元数据
    all_image_data = fetch_all_image_metadata(api_params)
    original_count = len(all_image_data)
    
    # <-- 新增：根据 --image-count 参数截取图片列表
    if args.image_count > 0 and args.image_count < original_count:
        print(f"[*] 用户指定下载前 {args.image_count} 张图片，将从总共 {original_count} 张中选取。")
        all_image_data = all_image_data[:args.image_count]

    total_images = len(all_image_data)
    if total_images == 0:
        print("[*] 没有找到任何图片，程序退出。")
        return

    # 2. 使用线程池进行下载和处理
    print(f"[2/3] 开始使用 {args.threads} 个线程进行下载和转换...")
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = [executor.submit(process_and_download_image, img_data, temp_image_dir, args.jpeg_quality) for img_data in all_image_data]
        
        for future in as_completed(futures):
            result = future.result()
            if result:
                print(result)

    print("\n[*] 所有图片处理完成。")
    
    # 3. 创建ZIP压缩包 (如果需要)
    if not args.no_zip:
        zip_filename = f"civitai_{args.username}_images.zip"
        zip_filepath = os.path.join(args.output_dir, zip_filename)
        create_zip_archive(temp_image_dir, zip_filepath)
    else:
        print("[*] 已跳过创建ZIP压缩包。JPEG文件保存在 " + temp_image_dir)

    print("\n[SUCCESS] 所有任务已完成！")

if __name__ == "__main__":
    main()
