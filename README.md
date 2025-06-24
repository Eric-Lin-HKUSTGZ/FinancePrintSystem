# FinancePrintSystem

## 项目简介
港科广学校财务处需求，开发基于GUID的票据文件处理系统，支持通过GUID进行票据文件的合并、下载、打印及交互操作。该功能将嵌入到学校财务系统（PBMS）。

## 项目结构
```
FinancePrintSystem/
├── pycache/
├── bmo/ # 业务模块组件
├── file/ # 文件存储目录
├── static/ # 静态资源文件
├── .gitignore
├── get_guid_files.py # 测试脚本：通过GUID下载文件
├── run_demo.py # 接收POST请求的GUID并交互，不支持并发
├── run_demo_symlink.py # 接收POST请求的GUID并交互,支持多用户并发请求
├── run_demo_symlink_simple.py # run_demo_symlink的代码精简版本，鲁棒性更好
├── run_demo_mp.py # 接收POST请求的GUID并交互,支持多用户并发请求(偶发bug)
├── run_demo_mp9_0616.py # 接收POST请求的GUID并交互,支持多用户并发请求（偶发bug）
├── run_bpms_guid_hand.py # 手动输入GUID进行交互
├── test_api.py # API测试脚本：模拟单用户POST请求
├── test_mp.py # API测试脚本：模拟多用户POST请求
└── nohup.out # 服务日志输出文件
```


## 核心功能
1. **GUID文件下载**：通过`get_guid_files.py`测试指定GUID对应的文件下载。
2. **API请求处理**：通过`run_demo_symlink_simple.py`接收POST请求中的GUID，并在前端展示交互流程, 实际部署。
3. **自动化测试**：通过`test_api.py`模拟用户请求，验证API服务的可用性。
4. **自动化测试**：通过`test_mp.py`模拟多用户同时请求，验证run_demo_mp中API服务的可用性。


## 快速开始

### 环境要求
- Python 3.6+
- 依赖库：根据项目实际依赖补充（如Flask、requests等）

### 启动服务
两种模式根据实际需求选择一种即可
```bash
# 启动手动输入模式（前端交互）
python run_bpms_guid_hand.py

# 启动API接收模式（POST请求处理）
python run_demo_symlink_simple.py
```

### 测试API服务
1. 使用test_mp.py脚本测试：
```bash
python test_mp.py
```

2. 或通过curl命令直接发送POST请求：
```bash
curl -X POST \
-H "Content-Type: application/json" \
-d '{"guid":"c965be9f9c1d449d9e50fed330150d7a"}' \
-s -i http://10.120.20.213:24360/api/set_guid
```

## 使用说明
1. 手动输入模式：运行run_bpms_guid_hand.py后，在前端界面直接输入GUID并提交。  
2. API模式：运行run_demo_symlink_simple.py后，通过POST请求向/api/set_guid发送JSON格式的GUID数据。  

## 备注
1. run_bpms_guid_hand根据文件类型分为PDF文件和图片文件，run_demo根据业务要求分为发票和附件。
2. run_demo_mp再多用户使用场景有bug，而run_bpms_guid_demo方便用户测试，但无法多用户使用。
3. run_demo_symlink_simple在多用户使用下性能稳定。

## 三个版本API服务对比（run_demo_mp, run_demo_mp9_0616, run_demo_symlink_simple）
| 特性         | run_demo_mp.py (问题严重)                             | run_demo_m9_0616.py (偶现Bug)                          | run_demo_symlink_simple.py (稳定可靠)                          |
| ------------ | ----------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------ |
| **文件存储** | 文件被下载到每个用户的会话目录中。                    | 文件被下载到每个用户的会话目录中。                     | 文件只下载一次，存入一个中央guid_files目录。           |
| **数据共享** | 在不同会话间物理复制文件。                            | 同样在会话间物理复制文件。                             | 在会话目录中创建轻量的软链接(symlinks)指向中央文件。   |
| **会话依赖** | 强依赖。所有后来的用户都依赖第一个用户的会话目录中的文件。 | 强依赖。与run_demo_mp有相同的依赖问题。               | 无依赖。所有会话完全独立，互不影响。                  |
| **运行效率** | 低。文件被大量复制，磁盘I/O和空间占用高。             | 低。同样存在文件复制问题，效率低下。                   | 高。没有文件冗余，I/O开销极小。                       |
| **健壮性**   | 差。极易产生竞争条件、会话清理后文件丢失、数据混淆等问题。 | 差。核心缺陷未修复，仅改进了部分文件转换逻辑，治标不治本。 | 高。从根本上消除了文件管理的竞争条件和数据丢失风险。 |

### run_demo_symlink_simple.py 的关键改进之处
run_demo_symlink_simple.py 通过彻底重构文件的存储和访问方式，完美地解决了上述所有问题。以下是使其能够在多用户场景下稳定运行的具体代码改进：

1. 集中式的、永久的文件存储

这是最重要的改变：为所有下载的文件提供一个唯一的、持久化的存储位置。
代码实现:
```python
GUID_FILE_DIR = "./test_file/guid_files"
os.makedirs(GUID_FILE_DIR, exist_ok=True)
```
带来的好处: 文件不再被下载到临时的会话文件夹中，而是每个文件只下载一次，并存放在这个永久的 guid_files 目录里。这个目录与任何用户会话都无关，因此不会被自动清理。

2. “只下载一次”的智能逻辑

download_file 函数被重写，变得更加智能。它会先检查文件是否存在，避免了重复下载，这对性能和稳定性至关重要。
代码实现 (download_file 函数):
```python
def download_file(...):
    # 定义在中央存储区的真实路径
    guid_dir = os.path.join(GUID_FILE_DIR, guid)
    os.makedirs(guid_dir, exist_ok=True)
    real_file_path = os.path.join(guid_dir, filename)

    # 如果文件已在中央存储区存在，则不再下载
    if os.path.exists(real_file_path):
        # 立即返回已存在文件的路径
        return main_file_info, []

    # 否则，下载文件到中央存储区
    # ... 下载逻辑 ...
    with open(real_file_path, 'wb') as f:
        f.write(chunk)
```
带来的好处: 当多个用户请求同一个 guid 时，只有第一个用户的请求会触发下载。所有后续请求都会立即找到文件并继续处理，这使得应用响应更快，并从源头上避免了网络和磁盘I/O的竞争。

3. 使用软链接替代文件复制（脚本名称的由来）

这是画龙点睛的一笔。新脚本不再浪费资源去复制文件，而是创建轻量的“快捷方式”（即软链接）。
代码实现 (copy_files_to_sessions 和 ensure_symlink):
```python
# 这个函数不再复制文件，而是创建链接
async def copy_files_to_sessions(...):
    for session_id in all_session_ids:
        # ... 判断目标目录 ...
        # 调用新函数创建软链接
        ensure_symlink(str(target_dir), guid, filename)

# 新增的工具函数
def ensure_symlink(session_file_dir, guid, filename):
    real_file_path = os.path.abspath(os.path.join(GUID_FILE_DIR, guid, filename))
    link_path = os.path.join(session_file_dir, filename)
    if os.path.lexists(link_path): # lexists能判断软链接本身
        os.remove(link_path)
    # os.symlink(...) 创建“快捷方式”
    os.symlink(real_file_path, link_path)
    return link_path
```
### 统一管理+软链接的好处:

会话解耦: 每个用户的会话现在是完全独立的。它们的文件夹里只包含指向中央文件的链接。如果一个会话被删除，仅仅是这些链接被移除了，而 guid_files 里的真实文件安然无恙，可供其他用户继续使用。这个改动彻底消除了“文件丢失”和“数据混淆”的bug。

效率提升: 创建一个软链接几乎是瞬时的，并且不占用磁盘空间。相比之下，复制大文件既慢又浪费磁盘。这使得系统在高并发下的性能表现得到巨大提升。

### 总结
run_demo_symlink_simple.py 之所以稳定可靠，其秘诀在于一个优雅的架构设计思想：“数据集中存放，访问虚拟化”。通过将文件集中存储，并为每个会话提供独立的、轻量级的软链接，它修复了另外两个脚本中存在的根本性设计缺陷，最终实现了一个健壮、高效且可靠的多用户服务。