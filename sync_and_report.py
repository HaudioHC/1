# sync_and_report.py
import os
import json
import argparse
from datetime import datetime

# 假设你的下载脚本名为 new.py，并且可以导入其中的函数
# 为了方便，我们将需要的函数直接复制过来或进行重构
# --- 从 new.py 复制并稍作修改的核心函数 ---
import requests
import time
import zipfile
import io
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

API_BASE_URL = "https://civitai.com/api/v1/images"
download_progress = {"count": 0, "total": 0}
progress_lock = Lock()

def fetch_all_image_metadata(params):
    """获取指定创作者的所有图片元数据。"""
    print("[1/4] 正在从 Civitai API 获取所有图片元数据...")
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
            
    print(f"[*] API 查询完成，总共找到 {len(all_images)} 张图片。\n")
    return all_images

def download_and_convert_image(image_info, output_path, jpeg_quality):
    """下载、转换并保存单张图片。"""
    global download_progress
    
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
            download_progress["count"] += 1
            print(f"  [{download_progress['count']}/{download_progress['total']}] ✓ 下载并转换: {jpeg_filename}")
        
        return None
    except Exception as e:
        with progress_lock:
            download_progress["count"] += 1
        return f"  [{download_progress['count']}/{download_progress['total']}] ✗ 处理图片ID {image_id} 失败: {e}"

def create_zip_archive(source_dir, zip_filepath, files_to_zip):
    """将指定文件压缩成zip。"""
    print(f"\n[*] 正在将 {len(files_to_zip)} 个新图片文件创建到 ZIP 压缩包...")
    try:
        with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename in files_to_zip:
                filepath = os.path.join(source_dir, filename)
                if os.path.exists(filepath):
                    zf.write(filepath, arcname=filename)
        
        print(f"[*] 成功创建压缩包: {zip_filepath}")
        
    except Exception as e:
        print(f"  ✗ 创建ZIP时出错: {e}")

# --- 核心同步逻辑 ---

def load_manifest(manifest_path):
    """加载本地的状态文件。如果不存在，返回空字典。"""
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            print(f"[*] 成功加载本地清单: {manifest_path}")
            return json.load(f)
    print("[*] 未找到本地清单文件，将视为首次运行。")
    return {}

def save_manifest(manifest_path, data):
    """将最新的状态保存到文件。"""
    with open(manifest_path, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"[*] 已将最新清单保存到: {manifest_path}")

def generate_reports(reports_dir, new_images, deleted_images_data):
    """生成新增和删除的报告文件。"""
    os.makedirs(reports_dir, exist_ok=True)
    
    # 报告摘要
    summary_path = os.path.join(reports_dir, "summary.md")
    with open(summary_path, 'w') as f:
        f.write(f"# Civitai 同步报告 - {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write(f"- **新增图片**: {len(new_images)} 张\n")
        f.write(f"- **删除图片**: {len(deleted_images_data)} 张\n\n")
        
        f.write("## 新增图片详情\n")
        if new_images:
            for img in new_images:
                f.write(f"- ID: {img['id']}, URL: {img['url']}\n")
        else:
            f.write("无\n")
            
        f.write("\n## 删除图片详情\n")
        if deleted_images_data:
            for img in deleted_images_data:
                f.write(f"- ID: {img['id']}, Username: {img['username']}\n")
        else:
            f.write("无\n")
    print(f"[*] 报告摘要已生成: {summary_path}")

    # 简单的 txt 列表
    with open(os.path.join(reports_dir, "new_images_ids.txt"), 'w') as f:
        for img in new_images:
            f.write(f"{img['id']}\n")
            
    with open(os.path.join(reports_dir, "deleted_images_ids.txt"), 'w') as f:
        for img in deleted_images_data:
            f.write(f"{img['id']}\n")

def main(args):
    """主同步函数"""
    global download_progress

    output_dir = args.output_dir
    creator_username = args.username
    manifest_filename = f"{creator_username}_manifest.json"
    manifest_path = os.path.join(output_dir, manifest_filename)
    reports_dir = os.path.join(output_dir, "reports")
    
    # 1. 加载旧状态
    old_manifest = load_manifest(manifest_path)
    old_image_ids = set(old_manifest.keys())

    # 2. 获取当前所有图片数据
    api_params = {
        "username": creator_username, "limit": 200, "sort": "Newest",
        "period": "AllTime", "nsfw": "None" # 简化：总是获取所有数据进行对比
    }
    current_image_list = fetch_all_image_metadata(api_params)
    
    # 转换为以ID为键的字典，方便查找
    current_images_map = {str(img['id']): img for img in current_image_list}
    current_image_ids = set(current_images_map.keys())

    # 3. 对比差异
    print("\n[2/4] 正在比较新旧图片列表...")
    new_image_ids = current_image_ids - old_image_ids
    deleted_image_ids = old_image_ids - current_image_ids
    
    new_images = [current_images_map[id] for id in new_image_ids]
    deleted_images_data = [old_manifest[id] for id in deleted_image_ids]

    print(f"[*] 比较完成: {len(new_images)} 张新增, {len(deleted_images_data)} 张删除。")

    # 4. 生成报告
    if new_images or deleted_images_data:
        print("\n[3/4] 正在生成同步报告...")
        generate_reports(reports_dir, new_images, deleted_images_data)
    else:
        print("\n[3/4] 图片列表无变化，跳过生成报告。")

    # 5. 下载新图片并打包
    if new_images:
        print(f"\n[4/4] 发现 {len(new_images)} 张新图片，开始下载...")
        temp_download_dir = os.path.join(output_dir, "new_images_temp")
        os.makedirs(temp_download_dir, exist_ok=True)
        
        download_progress["total"] = len(new_images)
        download_progress["count"] = 0
        
        downloaded_filenames = []
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = [executor.submit(download_and_convert_image, img_data, temp_download_dir, args.jpeg_quality) for img_data in new_images]
            for future in as_completed(futures):
                result = future.result()
                if result: # 如果有错误信息
                    print(result)

        # 获取成功下载的文件名列表
        for img in new_images:
             downloaded_filenames.append(f"{img['username']}_{img['id']}.jpeg")

        # 创建ZIP压缩包
        zip_filename = f"civitai_{creator_username}_new_{datetime.utcnow().strftime('%Y%m%d')}.zip"
        zip_filepath = os.path.join(output_dir, zip_filename)
        create_zip_archive(temp_download_dir, zip_filepath, downloaded_filenames)

        # 清理临时文件
        print("[*] 正在清理临时下载目录...")
        for file in os.listdir(temp_download_dir):
            os.remove(os.path.join(temp_download_dir, file))
        os.rmdir(temp_download_dir)
    else:
        print("\n[4/4] 没有新图片需要下载。")

    # 6. 更新本地清单文件
    save_manifest(manifest_path, current_images_map)

    print("\n[SUCCESS] 同步任务完成！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synchronize and report Civitai creator images.")
    parser.add_argument("--username", type=str, required=True, help="The username of the Civitai creator.")
    parser.add_argument("--output-dir", type=str, default="./output", help="Directory for all outputs (manifest, reports, zips).")
    parser.add_argument("--threads", type=int, default=10, help="Number of download threads.")
    parser.add_argument("--jpeg-quality", type=int, default=85, help="JPEG conversion quality.")
    
    cli_args = parser.parse_args()
    main(cli_args)
