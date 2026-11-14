"""GitHub Actions 专用: 把 civitai 下载结果「打包 → 上传到 Release → 删掉本地文件」。

思路与 twitter_download 的 release_batch.py 完全一致:
- runner 磁盘只有十几 GB, 所以先按体积分卷, 每卷都小于 1900MiB(Release 单附件上限 2GiB)
- 每卷上传成功后立刻删掉本地 zip 以及这一卷对应的源文件, 磁盘峰值 = 尚未处理的文件 + 当前这一卷
- 用 `gh release upload --clobber` 覆盖同名资产, 所以同一个 run 重跑也能直接续传
- 沿用 gh 继承 stdio 的调用方式, 上传进度直接出现在 Actions 日志里

环境变量:
  GH_TOKEN / GITHUB_REPOSITORY   gh 认证(workflow 里用 ${{ github.token }} 注入)
  RELEASE_TAG                    必填, 例如 civitai-<run_id>
  RELEASE_TITLE / RELEASE_NOTES  可选, 建 Release 时用
  OUTPUT_DIR                     默认 ./output
  CREATOR_USERNAME               默认 civitai, 只用于资产命名

用法:
  python release_upload.py
"""

import glob
import os
import shutil
import subprocess
import sys
import time
import zipfile

LIMIT = 1900 * 1024 * 1024              # 单卷目标体积(GitHub 上限 2GiB, 留点余量)
HARD_LIMIT = 2 * 1024 * 1024 * 1024     # 超过这个体积直接报错, 不浪费一次上传
RESERVE = 300 * 1024 * 1024             # 打包时给系统留的余量
ZIPS_DIR = 'zips'
UPLOAD_RETRIES = 2                      # gh release upload 失败后的额外重试次数


class ReleaseError(Exception):
    """打包/上传环节的问题(通常是磁盘空间不足或附件超限)"""


def human(n):
    return f'{n / 1024 / 1024:.0f} MiB'


def free_space(path='.'):
    return shutil.disk_usage(path).free


def run_gh(args, capture=False, check=True):
    """调用 gh。capture=False 时继承 stdio, 这样上传进度能直接出现在 Actions 日志里"""
    print('$ gh ' + ' '.join(args), flush=True)
    if capture:
        r = subprocess.run(['gh'] + args, capture_output=True, text=True)
    else:
        r = subprocess.run(['gh'] + args)
    if check and r.returncode != 0:
        detail = ((r.stderr or '') + (r.stdout or '')).strip() if capture else ''
        raise ReleaseError(f'gh {" ".join(args)} 失败(退出码 {r.returncode}) {detail}')
    return r


def upload_with_retry(tag, asset_path):
    """上传单卷, 网络抖动时重试; 重试用 --clobber, 半截的资产会被覆盖"""
    last = None
    for attempt in range(1, UPLOAD_RETRIES + 2):
        try:
            run_gh(['release', 'upload', tag, asset_path, '--clobber'])
            return
        except ReleaseError as e:
            last = e
            if attempt <= UPLOAD_RETRIES:
                wait = 5 * attempt
                print(f'!! 上传 {os.path.basename(asset_path)} 第 {attempt} 次失败, '
                      f'{wait}s 后重试: {e}', flush=True)
                time.sleep(wait)
    raise ReleaseError(str(last))


def ensure_release(tag, title, notes):
    """提前建好 Release, 这样每卷上传完就能立刻删本地文件。
    重跑同一个 run 时 Release 已存在, 直接复用并追加资产(--clobber 覆盖同名资产)。"""
    r = run_gh(['release', 'view', tag], capture=True, check=False)
    if r.returncode == 0 and r.stdout.strip():
        print(f'Release {tag} 已存在, 直接往里面追加资产', flush=True)
        return False
    run_gh(['release', 'create', tag, '--title', title, '--notes', notes])
    return True


def collect_files(output_dir):
    """收集下载产物(jpeg 图片, 以及顺带生成的 manifest/json 等), 按路径排序保证可复现"""
    files = []
    for root, _, names in os.walk(output_dir):
        for name in names:
            files.append(os.path.join(root, name))
    return sorted(files)


def split_by_size(files):
    """按体积分组, 保证每组打包后都低于 LIMIT"""
    groups, current, size = [], [], 0
    for f in files:
        f_size = os.path.getsize(f)
        if current and size + f_size > LIMIT:
            groups.append(current)
            current, size = [], 0
        current.append(f)
        size += f_size
    if current:
        groups.append(current)
    return groups


def write_zip(zip_path, files, base_dir):
    """zip 内保留相对 output 的目录结构, 解压后就是 <username>/*.jpeg"""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, os.path.relpath(f, base_dir))


def pack_upload(files, tag, base_dir, creator):
    """分卷打包并上传; 每卷上传成功后就删掉本地 zip 和这一卷的源文件。返回已上传的资产名列表。"""
    os.makedirs(ZIPS_DIR, exist_ok=True)
    parts = split_by_size(files)
    uploaded = []

    for index, part in enumerate(parts, 1):
        suffix = '' if len(parts) == 1 else f'-part{index}'
        asset = f'civitai-{creator}-images{suffix}.zip'
        zip_path = os.path.join(ZIPS_DIR, asset)

        # 打包前先确认空间够: 磁盘满时直接给出人话, 而不是写到一半抛 Errno 28
        need = sum(os.path.getsize(f) for f in part) + RESERVE
        if free_space() < need:
            raise ReleaseError(
                f'磁盘只剩 {human(free_space())}, 打包 {asset} 至少需要 {human(need)}; '
                f'请调小 --limit / --scan-passes, 或减少单次下载的图片数后再跑')

        write_zip(zip_path, part, base_dir)
        zip_size = os.path.getsize(zip_path)
        if zip_size > HARD_LIMIT:
            raise ReleaseError(f'{asset} 体积 {human(zip_size)}, 超过 Release 单附件 2GiB 上限')
        print(f'已打包: {asset} ({human(zip_size)}, {len(part)} 个文件)', flush=True)

        upload_with_retry(tag, zip_path)
        uploaded.append(asset)
        os.remove(zip_path)             # 上传成功才删本地包; 失败时本地还留着

        # 这一卷的源文件也已经进了 Release, 立刻删掉:
        # 磁盘峰值 = 尚未处理的文件 + 当前这一卷, 而不是全部文件 + 全部 zip
        for f in part:
            try:
                os.remove(f)
            except OSError:
                pass

    shutil.rmtree(ZIPS_DIR, ignore_errors=True)
    return uploaded


def append_summary(lines):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf8') as f:
            f.write('\n'.join(lines) + '\n\n')
    except Exception as e:
        print(f'写入 job summary 失败: {e}', flush=True)


def main():
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    tag = os.environ.get('RELEASE_TAG', '')
    title = os.environ.get('RELEASE_TITLE') or 'Civitai Creator Images'
    notes = os.environ.get('RELEASE_NOTES') or ''
    output_dir = os.environ.get('OUTPUT_DIR') or 'output'
    creator = (os.environ.get('CREATOR_USERNAME') or 'civitai').strip() or 'civitai'
    if not tag:
        raise SystemExit('缺少 RELEASE_TAG 环境变量')

    if not os.path.isdir(output_dir):
        print(f'!! 产出目录不存在: {output_dir}, 本次没有可上传的内容', flush=True)
        append_summary(['## Civitai 图片下载', '', f'没有产出文件(`{output_dir}` 不存在), 未创建 Release。'])
        return

    files = collect_files(output_dir)
    total_size = sum(os.path.getsize(f) for f in files)
    print(f'本次共 {len(files)} 个文件 / {human(total_size)}, '
          f'Release: {tag}, 起始可用空间: {human(free_space())}', flush=True)

    if not files:
        print('!! 没有找到任何产出文件, 跳过打包上传', flush=True)
        if os.environ.get('GITHUB_ACTIONS'):
            print('::warning::没有产出文件, 未创建 Release', flush=True)
        append_summary(['## Civitai 图片下载', '', '没有产出文件, 未创建 Release。'])
        return

    if shutil.which('gh') is None:
        raise SystemExit('找不到 gh 命令行, ubuntu-latest 自带; 若被裁剪请先 apt-get install gh')

    created = ensure_release(tag, title, notes)
    print(f'Release {"已创建" if created else "复用已有"}: {tag} {repo}'.rstrip(), flush=True)

    uploaded = pack_upload(files, tag, output_dir, creator)

    url = f'https://github.com/{repo}/releases/tag/{tag}' if repo else tag
    print(f'\n已上传 {len(uploaded)} 个资产: ' + ', '.join(uploaded), flush=True)
    print(f'下载地址: {url}', flush=True)
    print(f'结束时可用空间: {human(free_space())}', flush=True)

    append_summary([
        '## Civitai 图片下载',
        '',
        f'- 创作者: `{creator}`',
        f'- 文件数: {len(files)} 个, 原始体积 {human(total_size)}',
        f'- 分卷: {len(uploaded)} 个(单卷均小于 2GiB)',
        f'- Release: {url}',
        '',
        '资产: ' + ', '.join(f'`{a}`' for a in uploaded),
        '',
        '> 每个分卷上传完成后即删除本地文件, 避免 runner 磁盘写满。',
    ])


if __name__ == '__main__':
    main()
