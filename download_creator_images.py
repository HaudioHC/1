# download_creator_images.py
import os
import requests
import argparse
import time

# Civitai API基础URL
API_BASE_URL = "https://civitai.com/api/v1/images"

def setup_arguments():
    """设置命令行参数"""
    parser = argparse.ArgumentParser(description="Download all images from a specific Civitai creator.")
    
    # --- 核心设置 ---
    parser.add_argument("--username", type=str, required=True, help="The username of the Civitai creator.")
    parser.add_argument("--output-dir", type=str, default="./civitai_downloads", help="Directory to save the downloaded images.")
    
    # --- API过滤与排序设置 (个性化) ---
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 201), metavar="[1-200]", help="Number of images to fetch per API call (default: 100, max: 200).")
    parser.add_argument("--nsfw", type=str, default="None", choices=["None", "Soft", "Mature", "X"], help="Filter by NSFW level. 'None' means SFW only.")
    parser.add_argument("--sort", type=str, default="Newest", choices=["Most Reactions", "Most Comments", "Newest"], help="How to sort the images.")
    parser.add_argument("--period", type=str, default="AllTime", choices=["AllTime", "Year", "Month", "Week", "Day"], help="Time period for sorting.")
    
    # --- 脚本行为设置 ---
    parser.add_argument("--skip-existing", action='store_true', help="Skip downloading files that already exist in the output directory.")
    
    return parser.parse_args()

def download_image(url, filepath):
    """下载单个图片并保存"""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()  # 如果请求失败则引发HTTPError
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"  ✓ Saved to {filepath}")
        return True
    except requests.exceptions.RequestException as e:
        print(f"  ✗ FAILED to download {url}. Reason: {e}")
        return False

def fetch_and_download_images(args):
    """主函数，负责获取API数据并循环下载"""
    # 确保输出目录存在
    output_path = os.path.join(args.output_dir, args.username)
    os.makedirs(output_path, exist_ok=True)
    print(f"[*] Saving images for '{args.username}' to '{output_path}'")

    # 构建API请求参数
    params = {
        "username": args.username,
        "limit": args.limit,
        "sort": args.sort,
        "period": args.period,
        "nsfw": args.nsfw,
    }

    # 使用分页链接进行循环，这是最稳健的方式
    next_url = API_BASE_URL
    page_count = 1
    total_downloaded = 0
    
    while next_url:
        print(f"\n[*] Fetching page {page_count}...")
        try:
            response = requests.get(next_url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"  ✗ FAILED to fetch API data. Reason: {e}")
            break
        
        # 获取图片列表
        items = data.get('items', [])
        if not items:
            print("[*] No more images found.")
            break

        print(f"  > Found {len(items)} images on this page.")

        # 遍历并下载图片
        for image in items:
            image_id = image.get('id')
            image_url = image.get('url')
            
            if not image_id or not image_url:
                continue

            # 从URL中猜测文件扩展名，默认为.png
            file_extension = os.path.splitext(image_url.split('?')[0])[-1] or '.png'
            if not file_extension.startswith('.'):
                file_extension = '.png' # 如果没有扩展名，则默认
            
            filename = f"{args.username}_{image_id}{file_extension}"
            filepath = os.path.join(output_path, filename)
            
            if args.skip_existing and os.path.exists(filepath):
                print(f"  - Skipping existing file: {filename}")
                continue

            print(f"  - Downloading image ID: {image_id}")
            if download_image(image_url, filepath):
                total_downloaded += 1
            
            # 礼貌性地暂停一下，避免请求过于频繁
            time.sleep(0.2)

        # 准备获取下一页
        # Civitai API现在使用cursor，下一页的URL在metadata中
        next_url = data.get('metadata', {}).get('nextPage')
        params = {} # 后续请求直接使用完整的nextPage URL，不再需要params
        page_count += 1
        
        # 如果没有下一页的链接，则结束循环
        if not next_url:
            print("\n[*] Reached the last page.")
            break

    print(f"\n[SUCCESS] Download complete. Total new images downloaded: {total_downloaded}")

if __name__ == "__main__":
    args = setup_arguments()
    fetch_and_download_images(args)
