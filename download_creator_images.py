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
        formatter_class=argparse.RawTextHelpFormatter  # 更好地显示帮助信息
    )
    
    # --- 核心设置 ---
    parser.add_argument("--username", type=str, required=True, help="The username of the Civitai creator.")
    parser.add_argument("--output-dir", type=str, default="./downloads", help="Directory to save the final zip file.")
    
    # --- API过滤与排序 ---
    parser.add_argument("--nsfw", type=str, default="All", choices=["All", "None", "Soft", "Mature", "X"],
                        help="Filter by NSFW level.\n'All' (default): Gets every image.\n'None': SFW only.")
    parser.add_argument("--sort", type=str, default="Newest", choices=["Most Reactions", "Most Comments", "Newest"],
                        help="Sorting order for the images.")
    parser.add_argument("--period", type=str, default="AllTime", choices=["AllTime", "Year", "Month", "Week", "Day"],
                        help="Time period for sorting.")
    
    # --- 性能与格式化 (新功能) ---
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 201), metavar="[1-200]",
                        help="Number of images to fetch per API call (default: 100).\nAPI allows between 1 and 200.")
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
            # 首次请求使用params，后续直接使用API返回的完整URL
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
        # 下载图片到内存
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        # 使用Pillow进行格式转换
        image_bytes = io.BytesIO(response.content)
        img = Image.open(image_bytes)
        
        # 如果图片包含透明通道(RGBA)，转换为RGB以保存为JPEG
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        
        # 保存为JPEG
        jpeg_filename = f"{username}_{image_id}.jpeg"
        jpeg_filepath = os.path.join(output_path, jpeg_filename)
        img.save(jpeg_filepath, 'jpeg', quality=jpeg_quality)

        # 线程安全地更新进度
        with progress_lock:
            download_count += 1
            # 实时播报进度
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
                        zf.write(filepath, arcname=file) # arcname避免在zip中包含完整路径
        
        print(f"[*] 成功创建压缩包: {zip_filepath}")
        
        # 清理单个JPEG文件
        print("[*] 正在清理临时文件...")
        for file in os.listdir(source_dir):
            if file.endswith('.jpeg'):
                os.remove(os.path.join(source_dir, file))
        os.rmdir(source_dir) # 删除临时子目录
        
    except Exception as e:
        print(f"  ✗ 创建ZIP或清理文件时出错: {e}")


def main():
    """主执行函数"""
    global total_images
    args = setup_arguments()
    
    # 准备路径
    # 创建一个临时子目录来存放jpeg文件，方便之后打包和清理
    temp_image_dir = os.path.join(args.output_dir, args.username)
    os.makedirs(temp_image_dir, exist_ok=True)
    
    # 准备API参数
    api_params = {"username": args.username, "limit": args.limit, "sort": args.sort, "period": args.period}
    if args.nsfw != "All":
        api_params["nsfw"] = args.nsfw

    # 1. 获取所有图片元数据
    all_image_data = fetch_all_image_metadata(api_params)
    total_images = len(all_image_data)
    if total_images == 0:
        print("[*] 没有找到任何图片，程序退出。")
        return

    # 2. 使用线程池进行下载和处理
    print(f"[2/3] 开始使用 {args.threads} 个线程进行下载和转换...")
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        # 提交所有任务
        futures = [executor.submit(process_and_download_image, img_data, temp_image_dir, args.jpeg_quality) for img_data in all_image_data]
        
        # 等待任务完成并处理错误
        for future in as_completed(futures):
            result = future.result()
            if result: # 如果函数返回了错误信息
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
