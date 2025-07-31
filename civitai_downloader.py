import os
import requests
import time

# Civitai API 端点
API_BASE_URL = "https://civitai.com/api/v1"

def get_all_creator_images(api_key, username, nsfw, sort):
    """
    通过分页获取指定创作者的所有图片信息。
    API 单次请求有数量限制，此函数会自动处理分页，直到获取所有图片。
    """
    all_images = []
    page_url = f"{API_BASE_URL}/images?username={username}&sort={sort}&limit=100"
    if nsfw is not None:
        page_url += f"&nsfw={str(nsfw).lower()}"
        
    headers = {"Authorization": f"Bearer {api_key}"}

    while page_url:
        try:
            print(f"正在从 URL 获取数据: {page_url.split('?')[0]}...")
            response = requests.get(page_url, headers=headers)
            response.raise_for_status()  # 如果请求失败 (例如 4xx 或 5xx 错误), 则抛出异常

            data = response.json()
            images = data.get('items', [])
            all_images.extend(images)
            
            # 获取下一页的链接以进行分页
            page_url = data.get('metadata', {}).get('nextPage')
            
            # 为防止 API 请求过于频繁，增加一个小的延时
            if page_url:
                time.sleep(1)

        except requests.exceptions.HTTPError as e:
            print(f"HTTP 错误: {e.response.status_code} - {e.response.text}")
            break
        except requests.exceptions.RequestException as e:
            print(f"请求时发生错误: {e}")
            break
            
    return all_images

def download_image(image_info, folder_path):
    """根据图片信息下载单个图片。"""
    image_url = image_info.get('url')
    image_id = image_info.get('id')
    creator_username = image_info.get('user', {}).get('username', 'unknown_creator')

    if not image_url or not image_id:
        print(f"信息不完整，跳过下载: {image_info}")
        return False

    try:
        # 提取原始文件名和扩展名，构建一个清晰的文件名
        # 格式：创作者名_图片ID.扩展名
        file_extension = os.path.splitext(image_url.split('?')[0])[-1] or '.png'
        filename = f"{creator_username}_{image_id}{file_extension}"
        file_path = os.path.join(folder_path, filename)
        
        if os.path.exists(file_path):
            print(f"文件已存在，跳过: {filename}")
            return True

        print(f"正在下载: {filename}")
        response = requests.get(image_url, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True

    except requests.exceptions.RequestException as e:
        print(f"下载图片失败 {image_url}: {e}")
        return False

def main():
    # --- 个性化配置区域 ---

    # 1. 从 GitHub Secrets 中获取 API 密钥 (无需修改)
    API_KEY = os.getenv("CIVITAI_API_KEY")
    if not API_KEY:
        raise ValueError("错误: CIVITAI_API_KEY 未设置。请在仓库的 Settings > Secrets 中添加。")

    # 2. 【必须修改】设置你想要下载的创作者的用户名
    CREATOR_USERNAME = "Nsdekk"  # <-- 在这里填入创作者的准确用户名

    # 3. 【可选】设置下载图片的保存文件夹
    DOWNLOAD_FOLDER = "downloaded_images"

    # 4. 【可选】丰富的个性化筛选设置
    # NSFW (Not Safe For Work) 内容筛选:
    # - True: 只下载 NSFW 图片
    # - False: 只下载非 NSFW 图片
    # - None: 下载所有类型的图片
    NSFW_FILTER = None

    # 图片排序方式:
    # - 'Newest': 最新发布
    # - 'Most Reactions': 最多反应
    # - 'Most Comments': 最多评论
    # - 'Most Buzz': 最多讨论
    SORT_ORDER = 'Most Reactions'
    
    # --- 脚本执行区域 (无需修改) ---
    
    print("--- Civitai 图片下载脚本 ---")
    print(f"创作者: {CREATOR_USERNAME}")
    print(f"排序方式: {SORT_ORDER}")
    print(f"NSFW 筛选: {NSFW_FILTER if NSFW_FILTER is not None else '所有'}")
    
    if CREATOR_USERNAME == "some_creator_username":
        print("\n!!! 警告: 请修改脚本中的 `CREATOR_USERNAME` 为你想要下载的创作者用户名。")
        return

    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

    images_to_download = get_all_creator_images(API_KEY, CREATOR_USERNAME, nsfw=NSFW_FILTER, sort=SORT_ORDER)

    if not images_to_download:
        print("未能找到该创作者的任何图片，或 API 请求失败。")
        return
        
    print(f"\n共找到 {len(images_to_download)} 张图片，开始下载...")
    
    success_count = 0
    fail_count = 0
    
    for image in images_to_download:
        if download_image(image, DOWNLOAD_FOLDER):
            success_count += 1
        else:
            fail_count += 1
        time.sleep(0.5) # 友好延时，避免IP被封

    print("\n--- 下载完成 ---")
    print(f"成功下载: {success_count} 张")
    print(f"下载失败: {fail_count} 张")
    print(f"所有图片已保存至 '{DOWNLOAD_FOLDER}' 文件夹。")


if __name__ == "__main__":
    main()
