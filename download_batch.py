"""GitHub Actions 专用: 一次跑多个 civitai 创作者(逐用户串行), 每下完一个就打包上传 Release 并删本地。

为什么要串行而不是「所有用户同时下」:
- runner 可用磁盘只有十几 GB。并行 N 个用户, 磁盘峰值就是 N 份媒体, 很容易写满,
  写满到连运行器自己的日志都写不进去。
- Civitai 公共 API 有速率限制, 多个用户同时轰会互相触发 429, 反而更慢。
- 真正的并发在单用户内部: 每个用户都会用 --threads 开线程池下图片。
  用户之间共享同一条出口带宽, 串行并不会损失吞吐, 只是把并发放在更合适的那一层。

每个用户跑独立的子进程(download_creator_images.py), 状态互不影响:
某个用户不存在 / 该等级下没有图, 只影响他自己, 后面的用户照常跑。

环境变量:
  USERS                必填, 逗号分隔的创作者列表, 例如 user1,user2,user3
  RELEASE_TAG          必填, 例如 civitai-<run_id>
  RELEASE_TITLE / RELEASE_NOTES   可选
  OUTPUT_DIR           默认 ./output
  其余透传给下载脚本: NSFW / SORT / PERIOD / LIMIT / THREADS /
                      JPEG_QUALITY / SCAN_PASSES / DOWNLOAD_RETRIES

用法:
  python download_batch.py
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

import release_upload as R

BASE_DIR = os.environ.get('OUTPUT_DIR') or 'output'
DOWNLOADER = 'download_creator_images.py'
MIN_FREE = 1500 * 1024 * 1024           # 低于这个剩余空间就不再开始下一个用户
DOWNLOAD_ATTEMPTS = 2                   # 单个用户下载的总尝试次数(仅对「一张都没下成」重试)
UPLOAD_ATTEMPTS = 2                     # 单个用户整批上传的总尝试次数(卷内另有退避重试)


def parse_users(raw):
    users, seen = [], set()
    for item in raw.replace('，', ',').split(','):
        name = item.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            users.append(name)
    return users


VALID_NSFW = ('All', 'None', 'Soft', 'Mature', 'X')


def nsfw_level():
    """界面上传下来的是 'All (所有等级)' 这种带括号文案, 这里取出首词并校验; 非法就退回 X"""
    raw = (os.environ.get('NSFW') or 'X').strip()
    token = raw.split()[0] if raw.split() else 'X'
    return token if token in VALID_NSFW else 'X'


def download_user(user):
    """把单个用户交给下载脚本(独立进程), 参数全部透传"""
    cmd = [
        sys.executable, DOWNLOADER,
        '--username', user,
        '--nsfw', nsfw_level(),
        '--sort', os.environ.get('SORT') or 'Newest',
        '--period', os.environ.get('PERIOD') or 'AllTime',
        '--limit', str(os.environ.get('LIMIT') or 100),
        '--threads', str(os.environ.get('THREADS') or 10),
        '--jpeg-quality', str(os.environ.get('JPEG_QUALITY') or 92),
        '--scan-passes', str(os.environ.get('SCAN_PASSES') or 3),
        '--download-retries', str(os.environ.get('DOWNLOAD_RETRIES') or 3),
        '--output-dir', BASE_DIR,
        '--no-zip',
    ]
    print(f'\n===== 开始下载: {user}  (可用空间 {R.human(R.free_space())}) =====', flush=True)
    return subprocess.run(cmd).returncode


def run_one(user, tag):
    """下载 → 打包上传 → 清理。返回 (是否成功, 失败原因, 已上传资产)

    重试策略:
    - 下载: 退出码 1(一张都没下成, 通常是瞬时 429/网络问题) 自动再跑一次;
            退出码 2 表示该用户名下确实没有图片, 不浪费时间重试。
    - 上传: 单卷内部已有退避重试(release_upload.upload_with_retry);
            整用户级别再补一轮, 并把剩余文件改用 `-retry1` 命名,
            这样绝不会用新内容覆盖掉已经传上去的那几卷(--clobber 会覆盖同名资产)。
    """
    user_dir = os.path.join(BASE_DIR, user)

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        rc = download_user(user)
        if rc == 0:
            break
        if rc == 2:
            shutil.rmtree(user_dir, ignore_errors=True)
            return False, '该用户名下没有图片(或该 NSFW 等级/排序下无内容)', []
        if attempt >= DOWNLOAD_ATTEMPTS:
            print(f'!! {user} 下载连续 {attempt} 次失败, 不再重试', flush=True)
            break
        wait = 10 * attempt
        print(f'!! {user} 下载失败(退出码 {rc}), {wait}s 后重试一次', flush=True)
        time.sleep(wait)

    if not os.path.isdir(user_dir):
        return False, '下载脚本没有产出目录(用户不存在, 或该 NSFW 等级下没有图片)', []

    uploaded = []
    last_error = ''
    for ua in range(1, UPLOAD_ATTEMPTS + 1):
        # 每轮重新收集: 上一轮若已成功传掉几卷, 那些源文件已经删了
        remaining = R.collect_files(user_dir)
        if not remaining:
            break
        name = user if ua == 1 else f'{user}-retry{ua - 1}'
        try:
            uploaded += R.pack_upload(remaining, tag, user_dir, name)
            break
        except R.ReleaseError as e:
            last_error = str(e)
            if ua >= UPLOAD_ATTEMPTS:
                print(f'!! {user} 上传重试 {UPLOAD_ATTEMPTS} 次仍失败: {e}', flush=True)
                break
            wait = 5 * ua
            print(f'!! {user} 上传失败, {wait}s 后用 `-retry{ua}` 命名重传剩余文件: {e}', flush=True)
            time.sleep(wait)

    shutil.rmtree(user_dir, ignore_errors=True)

    if uploaded:
        if last_error:
            print(f'!! {user} 部分内容已上传({len(uploaded)} 卷), 但仍有文件未能上传: {last_error}',
                  flush=True)
        return True, '', uploaded
    return False, f'打包/上传失败: {last_error}' if last_error else '没有可下载的内容', []


def partial_failures():
    """读取下载脚本留下的失败清单: 说明这些用户「有图没下成」(不是整个用户失败)。
    重跑同一用户即可补齐, Release 里同名分卷会被 --clobber 覆盖。"""
    rows = []
    for path in sorted(glob.glob(os.path.join(BASE_DIR, '_*_failed.json'))):
        try:
            with open(path, encoding='utf8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        rows.append(f'- {data.get("username", "?")}: 成功 {data.get("success", 0)}/'
                    f'{data.get("total", 0)} 张, 失败 {len(data.get("failures", []))} 张'
                    f'(明细见 `{os.path.basename(path)}`)')
    return rows


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
    users = parse_users(os.environ.get('USERS', ''))
    if not tag:
        raise SystemExit('缺少 RELEASE_TAG 环境变量')
    if not users:
        raise SystemExit('USERS 是空的, 请给出逗号分隔的创作者列表')

    print(f'本次共 {len(users)} 个创作者: {", ".join(users)}', flush=True)
    print(f'Release: {tag}, 起始可用空间: {R.human(R.free_space())}', flush=True)

    if shutil.which('gh') is None:
        raise SystemExit('找不到 gh 命令行, ubuntu-latest 自带; 若被裁剪请先 apt-get install gh')

    created = R.ensure_release(tag, title, notes)
    print(f'Release {"已创建" if created else "复用已有"}: {tag} {repo}'.rstrip(), flush=True)

    ok, failed, uploaded_all = [], [], []
    for user in users:
        if R.free_space() < MIN_FREE:
            reason = f'磁盘只剩 {R.human(R.free_space())}, 停止继续下载'
            print(f'!! {reason}', flush=True)
            failed.append((user, reason))
            append_summary(['## Civitai 批量下载', '', f'**{reason}**, 在 `{user}` 处中止。',
                            '请减少用户数或收窄排序时间范围后重跑。'])
            break

        try:
            success, reason, assets = run_one(user, tag)
        except R.ReleaseError as e:
            success, reason, assets = False, str(e), []

        uploaded_all += assets
        if success:
            ok.append(user)
            print(f'== {user} 完成, 已上传 {len(assets)} 个资产: {", ".join(assets)}', flush=True)
        else:
            failed.append((user, reason))
            print(f'!! {user} 未能完成: {reason}', flush=True)
            if os.environ.get('GITHUB_ACTIONS'):
                print(f'::warning::{user} 未能下载: {reason}', flush=True)

    url = f'https://github.com/{repo}/releases/tag/{tag}' if repo else tag
    print(f'\n===== 批量结果: 成功 {len(ok)}/{len(users)} 个创作者 =====', flush=True)
    if uploaded_all:
        print('已上传资产: ' + ', '.join(uploaded_all), flush=True)
    print(f'下载地址: {url}', flush=True)
    print(f'结束时可用空间: {R.human(R.free_space())}', flush=True)

    lines = ['## Civitai 批量下载', '',
             f'成功 {len(ok)}/{len(users)}' + (f': {", ".join(ok)}' if ok else '')]
    if failed:
        lines += ['', f'失败/跳过 {len(failed)} 个:']
        lines += [f'- {name}: {reason}' for name, reason in failed]
    if uploaded_all:
        lines += ['', f'已上传 {len(uploaded_all)} 个资产(每个用户独立分卷):',
                  ', '.join(f'`{a}`' for a in uploaded_all)]
    partials = partial_failures()
    if partials:
        lines += ['', '部分图片未下成(重跑这些用户即可补齐, 同名分卷会被覆盖):'] + partials
    lines += ['', f'- Release: {url}', '',
              '> 每个用户上传完成后立即删除本地文件, 避免 runner 磁盘写满。']
    append_summary(lines)

    if not ok:
        raise SystemExit('所有创作者都未能下载, 请检查上面的原因')


if __name__ == '__main__':
    main()
