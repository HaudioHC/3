# sync_and_report.py
import os
import json
import argparse
from datetime import datetime
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
            # 打印将要请求的URL和参数，方便调试
            request_url = next_url if not is_first_page else f"{next_url}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
            print(f"  > Fetching: {request_url}")
            
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

def load_manifest(manifest_path):
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            print(f"[*] 成功加载本地清单: {manifest_path}")
            return json.load(f)
    print("[*] 未找到本地清单文件，将视为首次运行。")
    return {}

def save_manifest(manifest_path, data):
    with open(manifest_path, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"[*] 已将最新清单保存到: {manifest_path}")

def generate_reports(reports_dir, new_images, deleted_images_data):
    os.makedirs(reports_dir, exist_ok=True)
    
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

    with open(os.path.join(reports_dir, "new_images_ids.txt"), 'w') as f:
        for img in new_images:
            f.write(f"{img['id']}\n")
            
    with open(os.path.join(reports_dir, "deleted_images_ids.txt"), 'w') as f:
        for img in deleted_images_data:
            f.write(f"{img['id']}\n")

def main(args):
    global download_progress

    output_dir = args.output_dir
    creator_username = args.username
    # 清单文件现在需要包含nsfw和sort设置，以避免不同设置间的数据污染
    manifest_filename = f"{creator_username}_{args.nsfw}_{args.sort}_manifest.json"
    manifest_path = os.path.join(output_dir, manifest_filename)
    reports_dir = os.path.join(output_dir, "reports")
    
    old_manifest = load_manifest(manifest_path)
    old_image_ids = set(old_manifest.keys())

    # 使用传入的参数来构建API请求
    api_params = {
        "username": creator_username,
        "limit": 200, # 使用最大值以减少API请求次数
        "sort": args.sort,
        "period": "AllTime", # 同步时通常用AllTime
        "nsfw": args.nsfw
    }
    current_image_list = fetch_all_image_metadata(api_params)
    
    current_images_map = {str(img['id']): img for img in current_image_list}
    current_image_ids = set(current_images_map.keys())

    print("\n[2/4] 正在比较新旧图片列表...")
    new_image_ids = current_image_ids - old_image_ids
    deleted_image_ids = old_image_ids - current_image_ids
    
    new_images = [current_images_map[id] for id in new_image_ids]
    deleted_images_data = [old_manifest[id] for id in deleted_image_ids]

    print(f"[*] 比较完成: {len(new_images)} 张新增, {len(deleted_images_data)} 张删除。")

    if new_images or deleted_images_data:
        print("\n[3/4] 正在生成同步报告...")
        generate_reports(reports_dir, new_images, deleted_images_data)
    else:
        print("\n[3/4] 图片列表无变化，跳过生成报告。")

    if new_images:
        print(f"\n[4/4] 发现 {len(new_images)} 张新图片，开始下载...")
        temp_download_dir = os.path.join(output_dir, "new_images_temp")
        os.makedirs(temp_download_dir, exist_ok=True)
        
        download_progress["total"] = len(new_images)
        download_progress["count"] = 0
        
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {executor.submit(download_and_convert_image, img_data, temp_download_dir, args.jpeg_quality): img_data for img_data in new_images}
            
            downloaded_filenames = []
            for future in as_completed(futures):
                img_data = futures[future]
                try:
                    result = future.result()
                    if result:
                        print(result)
                    else:
                        downloaded_filenames.append(f"{img_data['username']}_{img_data['id']}.jpeg")
                except Exception as exc:
                    print(f"  ✗ 图片ID {img_data['id']} 生成异常: {exc}")

        if downloaded_filenames:
            zip_filename = f"civitai_{creator_username}_new_{datetime.utcnow().strftime('%Y%m%d')}.zip"
            zip_filepath = os.path.join(output_dir, zip_filename)
            create_zip_archive(temp_download_dir, zip_filepath, downloaded_filenames)

        print("[*] 正在清理临时下载目录...")
        for file in os.listdir(temp_download_dir):
            os.remove(os.path.join(temp_download_dir, file))
        os.rmdir(temp_download_dir)
    else:
        print("\n[4/4] 没有新图片需要下载。")

    save_manifest(manifest_path, current_images_map)

    print("\n[SUCCESS] 同步任务完成！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synchronize and report Civitai creator images.")
    parser.add_argument("--username", type=str, required=True, help="The username of the Civitai creator.")
    parser.add_argument("--output-dir", type=str, default="./output", help="Directory for all outputs.")
    
    # 添加回来的参数，并设置你想要的默认值
    parser.add_argument("--nsfw", type=str, default="X", choices=["None", "Soft", "Mature", "X"], help="Filter by NSFW level. Default: X")
    parser.add_argument("--sort", type=str, default="Newest", choices=["Most Reactions", "Most Comments", "Newest"], help="Sorting order for the images. Default: Newest")
    parser.add_argument("--threads", type=int, default=16, help="Number of download threads. Default: 16")
    parser.add_argument("--jpeg-quality", type=int, default=85, help="JPEG conversion quality.")
    
    cli_args = parser.parse_args()
    main(cli_args)
