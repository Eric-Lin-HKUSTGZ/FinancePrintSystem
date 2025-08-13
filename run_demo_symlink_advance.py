from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr
from gradio.routes import mount_gradio_app
import os
import uuid
import asyncio
from typing import Dict, List, Tuple, Optional
from PyPDF2 import PdfMerger
from PyPDF2.errors import PdfReadError
import tempfile
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image
import img2pdf
from img2pdf import Rotation 
import shutil
from pydantic import BaseModel
import zipfile
import rarfile
from rarfile import RarFile
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta
import time
from pathlib import Path
import pikepdf
import fitz  # PyMuPDF
import re

def clean_filename(filename: str) -> str:
    """
    清理文件名，移除或替换非法字符
    """
    # 移除或替换Windows和Unix文件系统的非法字符
    illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'
    filename = re.sub(illegal_chars, '_', filename)
    
    # 移除前后空格
    filename = filename.strip()
    
    # 确保文件名不为空
    if not filename:
        filename = "unnamed_file"
    
    # 限制文件名长度（避免过长）
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200-len(ext)] + ext
    
    return filename

def should_skip_mac_file(filename: str) -> bool:
    """
    判断是否应该跳过MAC系统特有的文件
    """
    # 转换为小写进行比较
    filename_lower = filename.lower()
    
    # 跳过MAC系统特有的文件和目录
    mac_skip_patterns = [
        '__macosx',  # MAC系统元数据目录
        '.ds_store',  # MAC桌面服务文件
        'thumbs.db',  # Windows缩略图文件
        'desktop.ini',  # Windows桌面配置文件
        '.trashes',  # MAC回收站
        '.spotlight-v100',  # MAC Spotlight索引
        '.fseventsd',  # MAC文件系统事件
    ]
    
    # 检查是否匹配任何跳过模式
    for pattern in mac_skip_patterns:
        if pattern in filename_lower:
            return True
    
    # 跳过以点开头的隐藏文件（除了正常的文件扩展名）
    if filename.startswith('.') and not any(filename.endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff']):
        return True
    
    return False

def is_valid_file_for_extraction(filename: str) -> bool:
    """
    判断文件是否适合提取（排除目录、MAC系统文件等）
    """
    # 跳过目录
    if filename.endswith('/') or filename.endswith('\\'):
        return False
    
    # 跳过MAC系统特有文件
    if should_skip_mac_file(filename):
        return False
    
    # 只处理常见的文档和图片文件
    valid_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.txt', '.rtf'}
    file_ext = os.path.splitext(filename)[1].lower()
    
    return file_ext in valid_extensions

def deduplicate_archive_files(file_list: list) -> list:
    """
    去重压缩文件中的重复条目，优先保留非MAC系统文件
    """
    seen_files = {}
    deduplicated = []
    
    for file_info in file_list:
        filename = file_info.filename.lower()
        
        # 跳过MAC系统文件
        if should_skip_mac_file(filename):
            continue
        
        # 获取文件的基本名称（不含路径）
        base_name = os.path.basename(filename)
        
        # 如果已经见过这个文件，跳过重复项
        if base_name in seen_files:
            print(f"跳过重复文件: {filename}")
            continue
        
        seen_files[base_name] = True
        deduplicated.append(file_info)
    
    return deduplicated

# 本地应用配置
APP_HOST = "0.0.0.0"

# APP_PORT = 9999
# MAP_API_PORT = 18561
APP_PORT = 9998
MAP_API_PORT = 18562
# Gradio后端调用自身API时使用的基础URL
APP_INTERNAL_BASE_URL = f"http://127.0.0.1:{APP_PORT}"

# 外部服务配置
MAP_API_HOST = "10.120.20.213"
# MAP_API_HOST = "10.120.20.176"
MAP_API_BASE_URL = f"http://{MAP_API_HOST}:{MAP_API_PORT}"

# 配置参数
AUTH_USER = "brgpt"
AUTH_PASS = "jiyMBV432-HAS98"
BASE_URL = "https://pbms.hkust-gz.edu.cn"
BASE_STATIC_DIR = Path("./test_file")
TEMP_DIR = tempfile.gettempdir()
GUID_FILE_DIR = BASE_STATIC_DIR / "guid_files"
SESSIONS_DIR = BASE_STATIC_DIR / "sessions"

# 创建基础目录
os.makedirs(BASE_STATIC_DIR, exist_ok=True)
os.makedirs(GUID_FILE_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

app = FastAPI()

# 用户会话管理类（参考main.py的设计）
class UserSession:
    def __init__(self, session_id: str, guid: str):
        self.session_id = session_id
        self.guid = guid
        # 创建基于session_id的目录结构
        self.base_dir = SESSIONS_DIR
        self.session_dir = self.base_dir / session_id
        self.file_dir = self.session_dir / "file"
        self.brno_dir = self.session_dir / "brno"
        self.merged_dir = self.session_dir / "merged"
        
        # 创建所有必要的目录
        self.file_dir.mkdir(parents=True, exist_ok=True)
        self.brno_dir.mkdir(parents=True, exist_ok=True)
        self.merged_dir.mkdir(parents=True, exist_ok=True)
        
        self.files: List[Dict] = []
        self.brno = ""
        self.processing = True
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
    
    def add_files(self, files: List[Dict]):
        """添加文件到会话"""
        self.files = files
        self.last_accessed = datetime.now()
    
    def set_brno(self, brno: str):
        """设置BRNO编号"""
        self.brno = brno
        self.last_accessed = datetime.now()
    
    def set_processing_complete(self):
        """标记处理完成"""
        self.processing = False
        self.last_accessed = datetime.now()
    
    def get_files(self) -> List[Dict]:
        """获取文件列表，只返回物理存在的文件"""
        self.last_accessed = datetime.now()
        return [f for f in self.files if os.path.exists(f["path"])]
    
    def get_file_dir(self) -> Path:
        """获取普通文件存储目录"""
        return self.file_dir
    
    def get_brno_dir(self) -> Path:
        """获取BRNO文件存储目录"""
        return self.brno_dir
        
    def get_merge_dir(self) -> Path:
        """获取合并文件存储目录"""
        return self.merged_dir

# 改进的并发状态管理
class ConcurrentStateManager:
    def __init__(self):
        self.user_sessions: Dict[str, UserSession] = {}  # session_id -> UserSession
        self.guid_sessions: Dict[str, set] = {}  # guid -> set of session_ids
        self.guid_files: Dict[str, Dict] = {}  # guid -> {"files": [...], "processing": True/False, "brno": ...}
        self.guid_locks: Dict[str, asyncio.Lock] = {}
        self.state_lock = threading.Lock()
        
    def create_session(self, guid: str, client_info: str = None) -> str:
        """创建用户会话"""
        session_id = str(uuid.uuid4())
        user_id = f"{client_info or 'user'}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        
        with self.state_lock:
            # 创建用户会话
            user_session = UserSession(session_id, guid)
            self.user_sessions[session_id] = user_session
            
            # 记录GUID到会话的映射
            if guid not in self.guid_sessions:
                self.guid_sessions[guid] = set()
            self.guid_sessions[guid].add(session_id)
            
            # 初始化guid_files，防止并发
            if guid not in self.guid_files:
                self.guid_files[guid] = {"files": [], "processing": True, "brno": ""}
            
        print(f"[会话] 用户 {user_id[:25]}... 访问GUID {guid[:8]}..., 会话: {session_id[:8]}...")
        print(f"[状态] GUID {guid[:8]}... 当前有 {len(self.guid_sessions[guid])} 个用户访问")
        
        return session_id
    
    def get_session(self, session_id: str) -> Optional[UserSession]:
        """获取用户会话"""
        with self.state_lock:
            return self.user_sessions.get(session_id)
    
    def get_guid_data(self, session_id: str = None, guid: str = None) -> Optional[Dict]:
        """获取GUID数据，支持通过session_id或guid查询"""
        with self.state_lock:
            user_session = None
            
            if session_id:
                user_session = self.user_sessions.get(session_id)
            elif guid:
                # 通过GUID找到任一会话（用于共享数据）
                session_ids = self.guid_sessions.get(guid, set())
                if session_ids:
                    session_id = next(iter(session_ids))
                    user_session = self.user_sessions.get(session_id)
            
            if user_session:
                user_session.last_accessed = datetime.now()
                return {
                    "guid": user_session.guid,
                    "files": user_session.get_files(),
                    "brno": user_session.brno,
                    "processing": user_session.processing,
                    "created_at": user_session.created_at,
                    "users": self.guid_sessions.get(user_session.guid, set()),
                    "access_count": len(self.guid_sessions.get(user_session.guid, set()))
                }
            return None
    
    def update_guid_data(self, guid: str, files: List[Dict], brno: str = ""):
        """更新GUID数据 - 更新所有相关会话"""
        with self.state_lock:
            if guid not in self.guid_files:
                self.guid_files[guid] = {"files": [], "processing": True, "brno": ""}
            self.guid_files[guid]["files"] = files
            self.guid_files[guid]["brno"] = brno
            self.guid_files[guid]["processing"] = False
            # 更新所有会话
            for session_id in self.guid_sessions.get(guid, set()):
                if session_id in self.user_sessions:
                    user_session = self.user_sessions[session_id]
                    user_session.files = files
                    user_session.brno = brno
                    user_session.processing = False
            
            print(f"[更新] GUID {guid[:8]}... 数据已更新，共 {len(files)} 个文件，更新了 {len(self.guid_sessions.get(guid, set()))} 个会话")
    
    def cleanup_session(self, session_id: str):
        """安全清理会话"""
        with self.state_lock:
            if session_id in self.user_sessions:
                user_session = self.user_sessions[session_id]
                guid = user_session.guid
                
                # 从GUID映射中移除
                if guid in self.guid_sessions:
                    self.guid_sessions[guid].discard(session_id)
                    if not self.guid_sessions[guid]:
                        del self.guid_sessions[guid]
                        print(f"[清理] 清理GUID {guid[:8]}... 的映射")
                
                # 移除会话
                del self.user_sessions[session_id]
                print(f"[清理] 清理会话 {session_id[:8]}...")
        
        # 在锁外清理文件目录，避免影响其他操作
        clean_session_directory(session_id)
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self.state_lock:
            return {
                "total_guids": len(self.guid_sessions),
                "total_sessions": len(self.user_sessions),
                "guid_users": {
                    guid[:8] + "...": len(sessions) 
                    for guid, sessions in self.guid_sessions.items()
                }
            }

def clean_session_directory(session_id: str):
    """Recursively deletes the directory for a given session."""
    session_dir = SESSIONS_DIR / session_id
    if session_dir.exists() and session_dir.is_dir():
        try:
            shutil.rmtree(session_dir)
            print(f"[文件清理] 已删除会话目录: {session_dir}")
        except Exception as e:
            print(f"[文件清理] 删除目录 {session_dir} 失败: {e}")

# 全局状态管理器
state_manager = ConcurrentStateManager()

# 线程池用于并发文件处理
thread_pool = ThreadPoolExecutor(max_workers=10)

# 定义数据模型
class GuidRequest(BaseModel):
    guid: str

class SessionRequest(BaseModel):
    session_id: str

@app.post("/api/set_guid")
async def set_guid(request: GuidRequest):
    """设置当前GUID并自动加载文件"""
    try:
        # 创建用户会话
        session_id = state_manager.create_session(request.guid, "api_user")

        # 每次都强制触发下载
        print(f"[DEBUG] set_guid: always scheduling process_files_async for guid={request.guid}")
        asyncio.create_task(process_files_async(request.guid))
        guid_data = state_manager.get_guid_data(guid=request.guid)
        active_users = len(guid_data.get("users", set())) if guid_data else 1
        return JSONResponse(
            content={
                "status": "success",
                "message": f"GUID已更新为 {request.guid}，文件加载中（共{active_users}个用户访问）",
                "guid": request.guid,
                "session_id": session_id,
                "cached": False,
                "active_users": active_users
            }
        )
    except Exception as e:
        print(f"[API] 文件加载失败: {str(e)}")
        return JSONResponse(
            content={
                "status": "error",
                "message": f"文件加载失败: {str(e)}",
                "guid": request.guid
            },
            status_code=500
        )

async def process_files_async(guid: str):
    print(f"[DEBUG] process_files_async started for guid={guid}")
    downloaded_files = []
    brno_number = ""
    failed_files = []

    try:
        print(f"[处理] 开始处理GUID {guid[:8]}... 的文件下载")
        # 获取任一活跃会话作为下载的目标（文件会被复制到所有相关会话）
        session_ids = state_manager.guid_sessions.get(guid, set())
        if not session_ids:
            print(f"[错误] GUID {guid[:8]}... 没有活跃会话")
            return
        # 使用第一个会话进行文件下载
        target_session_id = next(iter(session_ids))
        user_session = state_manager.get_session(target_session_id)
        if not user_session:
            print(f"[错误] 找不到会话 {target_session_id[:8]}...")
            return
        # 处理GUID并下载文件
        brno_number, brno_items, file_items = await asyncio.get_event_loop().run_in_executor(
            thread_pool, process_guids, guid
        )
        # 并发下载文件，增加进度反馈
        total_files = len(brno_items) + len(file_items)
        print(f"[下载] 开始下载 {total_files} 个文件...")
        
        download_tasks = []
        for file_type, g, _ in brno_items:
            task = asyncio.create_task(download_file_async(file_type, g, user_session=user_session))
            download_tasks.append(task)
        for item in file_items:
            file_type, g, name, attachtype = item
            task = asyncio.create_task(download_file_async(file_type, g, name, attachtype, user_session=user_session))
            download_tasks.append(task)
        
        # 等待所有下载完成，增加超时时间
        try:
            # 根据文件数量动态调整超时时间
            base_timeout = 300  # 基础5分钟
            per_file_timeout = 60  # 每个文件额外1分钟
            total_timeout = base_timeout + (total_files * per_file_timeout)
            
            print(f"[下载] 设置总超时时间: {total_timeout} 秒")
            results = await asyncio.wait_for(
                asyncio.gather(*download_tasks, return_exceptions=True),
                timeout=total_timeout
            )
        except asyncio.TimeoutError:
            print(f"[TIMEOUT] GUID {guid[:8]}... 文件下载超时 ({total_timeout}秒)")
            # 取消所有未完成的任务
            for task in download_tasks:
                if not task.done():
                    task.cancel()
            results = []
        for result in results:
            if isinstance(result, Exception):
                print(f"[下载] GUID {guid[:8]}... 文件下载错误: {result}")
            elif result:
                file_info, extracted_files = result
                if file_info:
                    if os.path.exists(file_info["path"]):
                        downloaded_files.append(file_info)
                    else:
                        failed_files.append(file_info.get("filename", "unknown"))
                    for ef in extracted_files:
                        if os.path.exists(ef["path"]):
                            downloaded_files.append(ef)
                        else:
                            failed_files.append(ef.get("filename", "unknown"))
        all_session_ids = state_manager.guid_sessions.get(guid, set())
        if len(all_session_ids) > 1:
            await copy_files_to_sessions(downloaded_files, all_session_ids, target_session_id)
        downloaded_files = deduplicate_files(downloaded_files)
        state_manager.update_guid_data(guid, downloaded_files, brno_number)
        guid_data = state_manager.get_guid_data(guid=guid)
        active_users = len(guid_data.get("users", set())) if guid_data else 0
        print(f"[完成] GUID {guid[:8]}... 所有文件处理完成，共 {len(downloaded_files)} 个文件，{active_users} 个用户可用")
        if failed_files:
            print(f"[下载] GUID {guid[:8]}... 以下文件下载失败或丢失: {failed_files}")
    except Exception as e:
        print(f"[ERROR] process_files_async exception for guid={guid}: {e}")
        import traceback; traceback.print_exc()
    finally:
        print(f"[DEBUG] process_files_async finally for guid={guid}, files={len(downloaded_files)}")
        if downloaded_files:
            state_manager.update_guid_data(guid, downloaded_files, brno_number)
        else:
            with state_manager.state_lock:
                if guid in state_manager.guid_files:
                    state_manager.guid_files[guid]["processing"] = True

async def download_file_async(file_type: str, guid: str, decoded_name: str = None, attachtype: str = None, user_session: UserSession = None) -> Tuple[Optional[Dict], List[Dict]]:
    """异步下载文件"""
    user_session.last_accessed = datetime.now()
    
    # 增加超时控制，大文件需要更长时间
    try:
        # 根据文件类型设置不同的超时时间
        if file_type == "brno":
            timeout = 600  # BR单文件，10分钟超时
        else:
            timeout = 300  # 其他文件，5分钟超时
            
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                thread_pool, download_file, file_type, guid, decoded_name, attachtype, user_session
            ),
            timeout=timeout
        )
        return result
    except asyncio.TimeoutError:
        print(f"[超时] 文件下载超时: {file_type} {guid}")
        return None, []
    except Exception as e:
        print(f"[错误] 异步下载文件失败: {file_type} {guid}, 错误: {e}")
        return None, []

async def copy_files_to_sessions(downloaded_files: List[Dict], all_session_ids: set, source_session_id: str):
    """为所有相关会话创建软连接指向统一存储区"""
    try:
        for session_id in all_session_ids:
            target_session = state_manager.get_session(session_id)
            if not target_session:
                continue
            print(f"[软连接] 为会话 {session_id[:8]}... 创建文件软连接...")
            for file_info in downloaded_files:
                guid = file_info["guid"]
                filename = file_info["filename"]
                # 目标目录
                if file_info["type"] == "brno":
                    target_dir = target_session.get_brno_dir()
                else:
                    target_dir = target_session.get_file_dir()
                if file_info.get("attach_type"):
                    target_dir = target_dir / file_info["attach_type"]
                    target_dir.mkdir(exist_ok=True)
                ensure_symlink(str(target_dir), guid, filename)
    except Exception as e:
        print(f"[软连接] 创建软连接过程中出错: {e}")
        import traceback
        traceback.print_exc()

class CustomStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except RuntimeError:
            # 对于会话文件，不提供fallback
            if scope.get("path", "").startswith("/sessions"):
                raise HTTPException(status_code=404, detail="文件不存在")
            response = await super().get_response("index.html", scope)
        
        if isinstance(response, FileResponse):
            file_ext = os.path.splitext(response.path)[1].lower()
            if file_ext == ".pdf":
                response.headers["Content-Type"] = "application/pdf"
                # 移除下载提示，在浏览器中直接打开
                if "Content-Disposition" in response.headers:
                    del response.headers["Content-Disposition"]
            elif file_ext in ['.jpg', '.jpeg', '.png', '.gif']:
                # 确保图片文件正确显示
                response.headers["Content-Type"] = f"image/{file_ext[1:]}"
        return response

# 会话文件访问中间件
@app.middleware("http")
async def session_file_middleware(request, call_next):
    # 检查是否是会话文件访问
    if request.url.path.startswith("/sessions/"):
        path_parts = request.url.path.split("/")
        if len(path_parts) >= 3:
            session_id = path_parts[2]
            # 验证会话是否存在
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return JSONResponse(
                    status_code=404, 
                    content={"error": "会话不存在或已过期"}
                )
    
    response = await call_next(request)
    return response

# 挂载会话文件静态访问
app.mount("/sessions", CustomStaticFiles(directory=SESSIONS_DIR), name="sessions")
# 保留原有静态文件访问（用于其他资源）
app.mount("/static", CustomStaticFiles(directory=BASE_STATIC_DIR), name="static")

def process_guids(initial_guid: str) -> Tuple[str, List[Tuple[str, str, str]], List[Tuple[str, str, str, str]]]:
    auth = HTTPBasicAuth(AUTH_USER, AUTH_PASS)
    response = requests.post(
        f"{BASE_URL}/api/br/BRFileLists",
        params={"guid": initial_guid},
        auth=auth,
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    
    data = response.json()
    brno_guids = []
    file_guids = []
    brno_number = ""

    for item in data.get("Data", []):
        if "BrNo" in item and not brno_number:
            brno_number = item["BrNo"]
        if "Guid" in item:
            brno_guids.append(("brno", item["Guid"], "")) 
        
        for file in item.get("Files", []):
            if "Guid" in file:
                raw_name = file.get("FileName", "")
                raw_attachtype = file.get("AttachType", "")
                try:
                    decoded_name = raw_name.encode('latin-1').decode('utf-8')
                    decoded_attachtype = raw_attachtype.encode('latin-1').decode('utf-8')
                except:
                    decoded_name = raw_name
                    decoded_attachtype = raw_attachtype
                file_guids.append(("file", file["Guid"], decoded_name, decoded_attachtype))
    
    return brno_number, brno_guids, file_guids

def download_file(file_type: str, guid: str, decoded_name: str = None, attachtype: str = None, user_session: UserSession = None) -> Tuple[Optional[Dict], List[Dict]]:
    # 统一存储目录
    guid_dir = os.path.join(GUID_FILE_DIR, guid)
    os.makedirs(guid_dir, exist_ok=True)
    # 文件名
    filename = decoded_name or f"{file_type}_{guid}.pdf"
    filename = filename.replace('/', '_').replace('\\', '_')
    real_file_path = os.path.join(guid_dir, filename)

    # 如果已存在，先删除，确保下载的是最新文件
    if os.path.exists(real_file_path):
        try:
            os.remove(real_file_path)
        except Exception as e:
            print(f"[删除旧文件失败] {real_file_path}: {e}")

    # 下载并覆盖
    endpoint = "br/sysdownload" if file_type == "brno" else "file/download"
    url = f"{BASE_URL}/{endpoint}?g={guid}"
    auth = HTTPBasicAuth(AUTH_USER, AUTH_PASS)
    headers = {"Content-Type": "application/pdf; charset=utf-8"}

    try:
        # 增加超时时间，大文件需要更长时间
        timeout = 300  # 5分钟超时
        response = requests.post(
            url,
            auth=auth,
            headers=headers,
            stream=True,
            timeout=timeout
        )
        if response.status_code != 200:
            print(f"[下载失败] {url} status={response.status_code} filename={decoded_name} attachtype={attachtype} response={response.text}")
            return None, []

        # 获取文件大小信息
        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        
        print(f"[下载] 开始下载文件: {filename}, 大小: {total_size / (1024*1024):.2f} MB")
        
        # 使用更大的chunk size来提高下载效率
        chunk_size = 1024 * 1024  # 1MB chunks
        
        with open(real_file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    
                    # 每下载10MB打印一次进度
                    if total_size > 0 and downloaded_size % (10 * 1024 * 1024) < chunk_size:
                        progress = (downloaded_size / total_size) * 100
                        print(f"[下载] {filename}: {progress:.1f}% ({downloaded_size / (1024*1024):.1f} MB / {total_size / (1024*1024):.1f} MB)")
        
        print(f"[下载] 文件下载完成: {filename}")
        
        # 验证下载的文件大小
        if total_size > 0:
            actual_size = os.path.getsize(real_file_path)
            if actual_size != total_size:
                print(f"[警告] 文件大小不匹配: 期望 {total_size}, 实际 {actual_size}")
                # 如果大小不匹配，删除文件并返回失败
                os.remove(real_file_path)
                return None, []

        main_file_info = {
            "guid": guid,
            "filename": filename,
            "path": real_file_path,
            "type": file_type
        }
        if attachtype:
            main_file_info["attach_type"] = attachtype

        extracted_files = []
        # 新增：自动解压 zip/rar 文件
        if file_type == "file" and attachtype:
            file_ext = os.path.splitext(real_file_path)[1].lower()
            if file_ext in ['.zip', '.rar']:
                extract_dir = os.path.join(guid_dir, attachtype)
                os.makedirs(extract_dir, exist_ok=True)
                print(f"[解压] 开始解压 {file_ext} 文件到目录: {extract_dir}")
                try:
                    if file_ext == '.zip':
                        # 修复ZIP文件编码问题
                        with zipfile.ZipFile(real_file_path, 'r') as zip_ref:
                            # 获取ZIP文件中的文件列表并去重
                            file_list = deduplicate_archive_files(zip_ref.infolist())
                            for zip_info in file_list:
                                try:
                                    # 尝试多种编码方式解码文件名
                                    filename = None
                                    encodings_to_try = ['utf-8', 'gbk', 'gb2312', 'latin-1', 'cp437']
                                    
                                    for encoding in encodings_to_try:
                                        try:
                                            filename = zip_info.filename.encode('cp437').decode(encoding)
                                            break
                                        except (UnicodeDecodeError, UnicodeEncodeError):
                                            continue
                                    
                                    if filename is None:
                                        # 如果所有编码都失败，使用原始文件名
                                        filename = zip_info.filename
                                    
                                    # 清理文件名中的非法字符
                                    filename = clean_filename(filename)
                                    
                                    # 检查是否应该跳过此文件（MAC系统文件、目录等）
                                    if not is_valid_file_for_extraction(filename):
                                        print(f"跳过文件: {filename}")
                                        continue
                                    
                                    # 解压单个文件
                                    zip_ref.extract(zip_info, extract_dir)
                                    
                                    # 重命名解压后的文件（如果文件名被修改）
                                    original_path = os.path.join(extract_dir, zip_info.filename)
                                    new_path = os.path.join(extract_dir, filename)
                                    
                                    if original_path != new_path:
                                        # 确保目标目录存在
                                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                                        if os.path.exists(new_path):
                                            # 如果文件已存在，添加数字后缀
                                            base, ext = os.path.splitext(filename)
                                            counter = 1
                                            while os.path.exists(new_path):
                                                new_filename = f"{base}_{counter}{ext}"
                                                new_path = os.path.join(extract_dir, new_filename)
                                                counter += 1
                                        shutil.move(original_path, new_path)
                                    
                                    # 添加到提取文件列表
                                    extracted_files.append({
                                        "guid": str(uuid.uuid4()),
                                        "filename": os.path.basename(new_path),
                                        "path": new_path,
                                        "type": "file",
                                        "attach_type": attachtype
                                    })
                                    print(f"[解压] 成功提取ZIP文件: {os.path.basename(new_path)}")
                                    
                                except Exception as e:
                                    print(f"处理ZIP文件中的文件 {zip_info.filename} 失败: {str(e)}")
                                    continue
                                    
                    elif file_ext == '.rar':
                        # 修复RAR文件编码问题
                        with rarfile.RarFile(real_file_path, 'r') as rar_ref:
                            # 获取RAR文件中的文件列表并去重
                            file_list = deduplicate_archive_files(rar_ref.infolist())
                            for rar_info in file_list:
                                try:
                                    # 尝试多种编码方式解码文件名
                                    filename = None
                                    encodings_to_try = ['utf-8', 'gbk', 'gb2312', 'latin-1']
                                    
                                    for encoding in encodings_to_try:
                                        try:
                                            filename = rar_info.filename.encode('latin-1').decode(encoding)
                                            break
                                        except (UnicodeDecodeError, UnicodeEncodeError):
                                            continue
                                    
                                    if filename is None:
                                        # 如果所有编码都失败，使用原始文件名
                                        filename = rar_info.filename
                                    
                                    # 清理文件名中的非法字符
                                    filename = clean_filename(filename)
                                    
                                    # 检查是否应该跳过此文件（MAC系统文件、目录等）
                                    if not is_valid_file_for_extraction(filename):
                                        print(f"跳过文件: {filename}")
                                        continue
                                    
                                    # 解压单个文件
                                    rar_ref.extract(rar_info, extract_dir)
                                    
                                    # 重命名解压后的文件（如果文件名被修改）
                                    original_path = os.path.join(extract_dir, rar_info.filename)
                                    new_path = os.path.join(extract_dir, filename)
                                    
                                    if original_path != new_path:
                                        # 确保目标目录存在
                                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                                        if os.path.exists(new_path):
                                            # 如果文件已存在，添加数字后缀
                                            base, ext = os.path.splitext(filename)
                                            counter = 1
                                            while os.path.exists(new_path):
                                                new_filename = f"{base}_{counter}{ext}"
                                                new_path = os.path.join(extract_dir, new_filename)
                                                counter += 1
                                        shutil.move(original_path, new_path)
                                    
                                    # 添加到提取文件列表
                                    extracted_files.append({
                                        "guid": str(uuid.uuid4()),
                                        "filename": os.path.basename(new_path),
                                        "path": new_path,
                                        "type": "file",
                                        "attach_type": attachtype
                                    })
                                    print(f"[解压] 成功提取RAR文件: {os.path.basename(new_path)}")
                                    
                                except Exception as e:
                                    print(f"处理RAR文件中的文件 {rar_info.filename} 失败: {str(e)}")
                                    continue
                    
                    # 清理空目录
                    for root, dirs, files in os.walk(extract_dir, topdown=False):
                        for dir_name in dirs:
                            dir_path = os.path.join(root, dir_name)
                            try:
                                if not os.listdir(dir_path):  # 如果目录为空
                                    os.rmdir(dir_path)
                            except OSError:
                                pass  # 忽略删除目录时的错误
                    
                    print(f"[解压] 解压完成，共提取 {len(extracted_files)} 个有效文件")
                                
                except Exception as e:
                    print(f"解压失败: {str(e)}")
                    import traceback
                    traceback.print_exc()
        return main_file_info, extracted_files
    except Exception as e:
        print(f"下载文件失败: {str(e)}")
        return None, []

def image_to_pdf(image_path: str, output_dir: str) -> str:
    pdf_path = os.path.join(output_dir, os.path.splitext(os.path.basename(image_path))[0] + ".pdf")
    try:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_path, rotation=Rotation.ifvalid))
    except Exception as e:
        img = Image.open(image_path)
        img.save(pdf_path, "PDF", resolution=100.0)
    return pdf_path

SOFFICE_LOCK = threading.Lock()

def word_to_pdf(word_path: str, output_dir: str) -> str:
    with SOFFICE_LOCK:
        base = os.path.splitext(os.path.basename(word_path))[0]
        pdf_name = base + "_from_docx.pdf"  # 避免与现有PDF同名
        pdf_path = os.path.join(output_dir, pdf_name)
        
        # 为并发环境创建唯一临时输出目录
        with tempfile.TemporaryDirectory() as temp_out_dir:
            cmd = [
                'soffice', '--headless', '--convert-to', 'pdf',
                '--outdir', temp_out_dir, word_path
            ]
            try:
                # 增加详细的错误捕获
                result = subprocess.run(
                    cmd, check=True, timeout=60, # 增加60秒超时
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )
            except subprocess.CalledProcessError as e:
                # 抛出包含soffice具体错误的异常
                error_message = f"Soffice转换失败. 返回码: {e.returncode}\nstdout: {e.stdout}\nstderr: {e.stderr}"
                raise RuntimeError(error_message) from e
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"Soffice转换超时: {word_path}") from e

            # 查找在临时目录中生成的PDF文件
            generated_files = os.listdir(temp_out_dir)
            pdf_files = [f for f in generated_files if f.lower().endswith('.pdf')]

            if not pdf_files:
                raise RuntimeError(f"Soffice转换后未找到PDF文件. Word路径: {word_path}")

            # 将生成的PDF移动到最终的目标位置
            generated_pdf_path = os.path.join(temp_out_dir, pdf_files[0])
            shutil.move(generated_pdf_path, pdf_path)
            
            return pdf_path

def ensure_symlink(session_file_dir, guid, filename):
    real_file_path = os.path.abspath(os.path.join(GUID_FILE_DIR, guid, filename))
    link_path = os.path.join(session_file_dir, filename)
    # 如果已存在同名文件/软连接，先删除
    if os.path.lexists(link_path):
        os.remove(link_path)
    os.symlink(real_file_path, link_path)
    return link_path

def create_interface():
    custom_css = """
    :root { --primary: #2563eb; --secondary: #4f46e5; --accent: #f59e0b; }
    .guide-box { border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; background: #f8fafc; margin-bottom: 24px; }
    .guide-box h2 { margin: 0 0 16px 0 !important; font-size: 18px !important; color: var(--primary) !important; }
    .guide-box ol { margin: 0; padding-left: 20px; line-height: 1.6; }
    .guide-box li { margin-bottom: 8px; }
    .section-title { font-weight: 600 !important; color: var(--primary) !important; margin-bottom: 12px !important; }
    .selectors-row { gap: 24px !important; margin-top: 16px !important; }
    .selector-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; flex: 1; background: white; }
    .selector-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .selector-title { font-weight: 500; color: #1e293b; }
    .checkbox-group { max-height: 300px; overflow-y: auto; padding: 8px; border: 1px solid #e2e8f0; border-radius: 6px; }
    .success { color: #059669 !important; background: #ecfdf5; padding: 12px; border-radius: 6px; }
    .error { color: #dc2626 !important; background: #fef2f2; padding: 12px; border-radius: 6px; }
    .file-link a { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; background: var(--primary); color: white !important; border-radius: 6px; text-decoration: none !important; transition: all 0.2s; }
    .file-link a:hover { background: var(--secondary); transform: translateY(-1px); }
    .btn-group { gap: 8px !important; }
    .global-btn-row { display: flex; justify-content: center; gap: 16px; margin: 20px 0; }
    /* Increase font size for progress bar text */
    .progress-text { font-size: 1.1em !important; font-weight: bold !important; }
    
    /* 文件顺序管理样式 */
    .order-section { border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; background: #f8fafc; margin: 16px 0; }
    .order-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .order-title { font-weight: 600; color: var(--primary); }
    .order-controls { display: flex; gap: 8px; }
    .file-order-display { background: white; border: 1px solid #e2e8f0; border-radius: 6px; padding: 16px; min-height: 100px; }
    .file-order-display h2 { margin: 0 0 12px 0 !important; font-size: 16px !important; color: var(--primary) !important; }
    .file-order-display p { margin: 8px 0; line-height: 1.5; }
    """

    js_func = """
    function refresh() {
        const url = new URL(window.location);
        if (url.searchParams.get('__theme') !== 'light') {
            url.searchParams.set('__theme', 'light');
            window.location.href = url.href;
        }
    }
    """

    with gr.Blocks(title="PBMS文件合并工具", css=custom_css, js=js_func) as demo:
        with gr.Column(elem_classes="guide-box"):
            gr.Markdown("""
            ## 🚀 操作指南
            1. 等待系统通过API接收GUID并自动加载文件。
            2. 点击 **连接最新会话** 按钮以访问文件。
            3. 选择需要合并的文件类型，然后点击 **开始合并**。
            4. 在文件顺序管理区域可以拖拽调整文件合并顺序。
            5. 点击生成的文档名称即可预览。
            6. 若BR单内容更新，请关闭后重新打开。
            """)

        # 会话管理区域
        with gr.Row():
            with gr.Column(scale=3):
                session_id_display = gr.Textbox(
                    label="当前会话 ID",
                    value="未连接",
                    interactive=False
                )
            with gr.Column(scale=1):
                connect_btn = gr.Button("🔗 连接最新会话", variant="primary")
        
        # 会话状态管理
        session_state = gr.State({})
        status_display = gr.HTML(visible=False)
        
        # 新增：下载进度显示
        progress_display = gr.HTML(visible=False)

        # 添加全局全选和清除按钮
        with gr.Row(elem_classes="global-btn-row"):
            global_select_all = gr.Button("全选所有文件", variant="primary")
            global_clear_all = gr.Button("清除所有选择", variant="secondary")

        with gr.Row(equal_height=False, elem_classes="selectors-row"):
            with gr.Column(elem_classes="selector-card"):
                gr.Markdown("### BRNO文件", elem_classes="section-title")
                brno_selector = gr.CheckboxGroup(label="选择BRNO文件", elem_classes="checkbox-group")
            
            with gr.Column(elem_classes="selector-card"):
                with gr.Row(elem_classes="selector-header"):
                    gr.Markdown("### 附件文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        bill_select_all = gr.Button("全选", size="sm")
                        bill_clear_all = gr.Button("清空", size="sm")
                bill_selector = gr.CheckboxGroup(label="选择附件文件", elem_classes="checkbox-group")

            with gr.Column(elem_classes="selector-card"):
                with gr.Row(elem_classes="selector-header"):
                    gr.Markdown("### 发票文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        invoice_select_all = gr.Button("全选", size="sm")
                        invoice_clear_all = gr.Button("清空", size="sm")
                invoice_selector = gr.CheckboxGroup(label="选择发票文件", elem_classes="checkbox-group")
                # 新增：发票合并模式选择
                invoice_merge_mode = gr.Radio(
                    choices=[("1张/页", 1), ("2张/页", 2), ("4张/页", 4)],
                    value=1,
                    label="发票合并模式（每页几张）"
                )
            
            with gr.Column(elem_classes="selector-card"):
                with gr.Row(elem_classes="selector-header"):
                    gr.Markdown("### 境外票据文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        overseas_select_all = gr.Button("全选", size="sm")
                        overseas_clear_all = gr.Button("清空", size="sm")
                overseas_selector = gr.CheckboxGroup(label="选择境外票据文件", elem_classes="checkbox-group")
        
        # 新增：文件顺序管理区域
        with gr.Column(elem_classes="order-section"):
            with gr.Row(elem_classes="order-header"):
                gr.Markdown("### 📋 文件合并顺序管理", elem_classes="order-title")
                with gr.Row(elem_classes="order-controls"):
                    update_order_btn = gr.Button("🔄 更新顺序", variant="secondary", size="sm")
                    clear_order_btn = gr.Button("🗑️ 清空顺序", variant="secondary", size="sm")
            
            # 使用Gradio原生组件替代HTML
            file_order_display = gr.Markdown(
                value="请先选择文件，然后点击\"更新顺序\"按钮",
                elem_classes="file-order-display"
            )
        
        with gr.Row():
            merge_btn = gr.Button("✨ 开始合并", variant="primary", scale=0)
        
        with gr.Column():
            file_link = gr.HTML(visible=False)
            status_label = gr.HTML(visible=False)
            
        # 下载进度显示区域
        with gr.Column():
            progress_display = gr.HTML(visible=False)

        merge_order_state = gr.State([])
        file_order_state = gr.State([])  # 新增：存储文件顺序状态

        def connect_latest_session():
            """连接最新的会话"""
            try:
                import requests
                response = requests.get(f"{APP_INTERNAL_BASE_URL}/api/latest_session")
                if response.status_code == 200:
                    data = response.json()
                    session_id = data["session_id"]
                    return session_id
                else:
                    return ""
            except Exception as e:
                return ""

        def load_initial_files(session_state):
            # 获取或创建会话ID
            session_id = session_state.get('session_id')
            if not session_id:
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='warning'>⚠️ 请先连接会话或通过API设置GUID</div>", visible=True),
                    session_state
                ]
            
            # 获取用户会话
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='error'>❌ 会话不存在</div>", visible=True),
                    session_state
                ]
            
            if user_session.processing:
                # 检查GUID级别的数据（可能其他用户已处理完成）
                guid_data = state_manager.get_guid_data(guid=user_session.guid)
                active_users = len(guid_data.get("users", set())) if guid_data else 1
                
                # 检查是否有文件正在下载
                files = user_session.get_files()
                if files:
                    # 有文件但还在处理中，可能是大文件下载
                    total_size = sum(os.path.getsize(f["path"]) for f in files if os.path.exists(f["path"]))
                    size_mb = total_size / (1024 * 1024)
                    status_msg = f"<div class='info'>⏳ 文件加载中... (共{active_users}个用户访问)<br>📁 已下载: {len(files)}个文件 ({size_mb:.1f}MB)</div>"
                else:
                    status_msg = f"<div class='info'>⏳ 文件下载中... (共{active_users}个用户访问)<br>🔄 请耐心等待，大文件可能需要较长时间</div>"
                
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value=status_msg, visible=True),
                    session_state
                ]
            
            files = user_session.get_files()
            if not files:
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='warning'>⚠️ 未找到文件</div>", visible=True),
                    session_state
                ]
            
            allowed_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
            brno_files = []
            invoice_files = []
            bill_files = []
            overseas_files = []
            
            for f in files:
                file_ext = os.path.splitext(f["filename"])[1].lower()
                if file_ext not in allowed_extensions:
                    continue
                if f["type"] == "brno":
                    brno_files.append(f)
                elif f.get("attach_type") == "发票":
                    invoice_files.append(f)
                elif ("附件" in f.get("attach_type", "")) or ("合同" in f.get("attach_type", "")):
                    bill_files.append(f)
                elif f.get("attach_type") == "境外票据":
                    overseas_files.append(f)
            
            total_files = len(brno_files) + len(invoice_files) + len(bill_files) + len(overseas_files)
            guid_data = state_manager.get_guid_data(guid=user_session.guid)
            active_users = len(guid_data.get("users", set())) if guid_data else 1
            
            return [
                gr.update(choices=[(f["filename"], f["guid"]) for f in brno_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in invoice_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in bill_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in overseas_files]),
                gr.update(value=f"<div class='success'>✅ 文件已加载，共 {total_files} 个文件 ({active_users}个用户访问)</div>", visible=True),
                session_state
            ]

        def update_merge_order(brno, invoice, bill, overseas, prev_order):
            # 按照业务要求的顺序：BR单、附件、境外票据、发票
            selected = brno + bill + overseas + invoice
            new_order = [g for g in prev_order if g in selected]
            for g in selected:
                if g not in new_order:
                    new_order.append(g)
            return new_order

        def select_all_by_type(attach_type: str, session_state):
            """按类型全选文件"""
            session_id = session_state.get('session_id')
            if not session_id:
                return gr.update(value=[])
            
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return gr.update(value=[])
            
            files = user_session.get_files()
            allowed_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
            valid_guids = []
            for f in files:
                file_ext = os.path.splitext(f["filename"])[1].lower()
                if file_ext not in allowed_extensions:
                    continue

                if attach_type == "发票" and f.get("attach_type") == "发票":
                    valid_guids.append(f["guid"])
                elif attach_type == "附件" and ("附件" in f.get("attach_type", "") or "合同" in f.get("attach_type", "")):
                    valid_guids.append(f["guid"])
                elif attach_type == "境外票据" and f.get("attach_type") == "境外票据":
                    valid_guids.append(f["guid"])
            
            return gr.update(value=valid_guids)

        def select_all_global(session_state):
            """全选所有文件"""
            session_id = session_state.get('session_id')
            if not session_id:
                return [gr.update()]*4

            user_session = state_manager.get_session(session_id)
            if not user_session:
                return [gr.update()]*4

            files = user_session.get_files()
            allowed_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
            brno_guids = []
            bill_guids = []
            overseas_guids = []
            invoice_guids = []

            for f in files:
                file_ext = os.path.splitext(f["filename"])[1].lower()
                if file_ext not in allowed_extensions:
                    continue
                if f["type"] == "brno":
                    brno_guids.append(f["guid"])
                elif ("附件" in f.get("attach_type", "")) or ("合同" in f.get("attach_type", "")):
                    bill_guids.append(f["guid"])
                elif f.get("attach_type") == "境外票据":
                    overseas_guids.append(f["guid"])
                elif f.get("attach_type") == "发票":
                    invoice_guids.append(f["guid"])

            return [
                gr.update(value=brno_guids),
                gr.update(value=invoice_guids),
                gr.update(value=bill_guids),
                gr.update(value=overseas_guids)
            ]

        def clear_all_global():
            """清除所有选择"""
            return gr.update(value=[]), gr.update(value=[]), gr.update(value=[]), gr.update(value=[])

        def update_file_order(brno, invoice, bill, overseas, session_state):
            """更新文件顺序列表"""
            session_id = session_state.get('session_id')
            if not session_id:
                return gr.update(value="请先连接会话"), []
            
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return gr.update(value="会话不存在"), []
            
            files = user_session.get_files()
            # 按照业务要求的顺序：BR单、附件、境外票据、发票
            selected_guids = brno + bill + overseas + invoice
            
            if not selected_guids:
                return gr.update(value="请先选择文件"), []
            
            # 构建文件顺序列表
            file_order_items = []
            for i, guid in enumerate(selected_guids, 1):
                file = next((f for f in files if f["guid"] == guid), None)
                if file:
                    file_type = "BRNO" if file["type"] == "brno" else file.get("attach_type", "文件")
                    file_order_items.append({
                        "guid": guid,
                        "filename": file["filename"],
                        "type": file_type,
                        "order": i
                    })
            
            # 生成Markdown格式的列表
            markdown_content = "## 文件合并顺序\n\n"
            for item in file_order_items:
                markdown_content += f"**{item['order']}.** {item['filename']} ({item['type']})\n\n"
            
            return gr.update(value=markdown_content), file_order_items

        def clear_file_order():
            """清空文件顺序"""
            return gr.update(value="文件顺序已清空"), []

        def get_file_order_from_markdown(markdown_content):
            """从Markdown内容中提取文件顺序"""
            # 由于现在使用Markdown格式，我们直接使用selected_guids的顺序
            # 这个函数现在主要用于兼容性，实际顺序由selected_guids决定
            return []

        # 在merge_files_async函数中，使用用户选择的顺序来合并文件
        async def merge_files_async(selected_guids: list, session_state, invoice_merge_mode, file_order_markdown, progress: gr.Progress = gr.Progress()):
            session_id = session_state.get('session_id')
            if not session_id:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 请先连接会话</div>", visible=True),
                ]
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 会话不存在</div>", visible=True),
                ]
            if not selected_guids:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 请至少选择一个文件</div>", visible=True),
                ]
            
            user_session.last_accessed = datetime.now()
            files_to_merge = []
            failed_files = []
            files = user_session.get_files()
            
            # 使用 progress.tqdm 实时更新前端进度条
            for guid in progress.tqdm(selected_guids, desc="文件处理中"):
                file = next((f for f in files if f["guid"] == guid), None)
                if not file:
                    print(f"[合并] 用户会话 {session_id[:8]}..., 找不到文件GUID {guid}")
                    continue
                try:
                    # 逐个处理文件，以便更新进度
                    converted_path = await process_file_for_merge(file, user_session)
                    if converted_path:
                        files_to_merge.append((file, converted_path))
                    else:
                        failed_files.append(file.get("filename", "未知文件"))
                except Exception as e:
                    print(f"[合并] 会话 {session_id[:8]}... 处理文件 {file['filename']} 失败: {str(e)}")
                    failed_files.append(file["filename"])
            
            # 使用用户选择的顺序来合并文件
            print("files_to_merge:", files_to_merge)
            if not files_to_merge:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 所有选中的文件都处理失败，无法合并</div>", visible=True),
                ]
            
            # 直接使用selected_guids的顺序，这是用户选择的文件顺序
            # 文件已经按照用户选择的顺序排列在files_to_merge中
            
            progress(0.95, desc="正在合并PDF...")
            valid_files = []
            missing_files = []
            for file, file_path in files_to_merge:
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    valid_files.append((file, file_path))
                else:
                    missing_files.append(os.path.basename(file_path))
            if missing_files:
                failed_files.extend(missing_files)
            
            brno_number = user_session.brno
            output_filename = f"{brno_number}.pdf" if brno_number else f"merged_{uuid.uuid4()}.pdf"
            merge_dir = user_session.get_merge_dir()
            output_path = merge_dir / output_filename.replace("/", "_")
            valid_file_paths = set(os.path.abspath(f[1]) for f in valid_files)
            while os.path.abspath(str(output_path)) in valid_file_paths:
                output_filename = f"merged_{uuid.uuid4()}.pdf"
                output_path = merge_dir / output_filename
            
            # 合并顺序：根据用户选择的顺序
            brno_files = [f for f in valid_files if f[0]["type"] == "brno"]
            bill_files = [f for f in valid_files if f[0].get("attach_type", "").find("附件") != -1]
            overseas_files = [f for f in valid_files if f[0].get("attach_type") == "境外票据"]
            invoice_files = [f for f in valid_files if f[0].get("attach_type") == "发票"]
            
            # 合并逻辑
            invoice_paths = [f[1] for f in invoice_files]
            other_paths = [f[1] for f in brno_files + bill_files + overseas_files]
            
            def merge_pdfs_with_pikepdf(valid_files, output_path, failed_files):
                with pikepdf.Pdf.new() as merged_pdf:
                    for file_path in valid_files:
                        try:
                            src = pikepdf.Pdf.open(file_path)
                            merged_pdf.pages.extend(src.pages)
                        except Exception as e:
                            failed_files.append(os.path.basename(file_path))
                            print(f"[pikepdf合并失败] {file_path}: {e}")
                    merged_pdf.save(str(output_path))
            
            def merge_pdfs_nup(pdf_paths, output_path, n_per_page=2):
                a4_width, a4_height = fitz.paper_size("a4")
                doc = fitz.open()
                images = []
                for pdf_path in pdf_paths:
                    src = fitz.open(pdf_path)
                    page = src[0]
                    if n_per_page == 4:
                        # 先转图片再旋转
                        pix = page.get_pixmap(dpi=200)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        img = img.rotate(90, expand=True)  # PIL逆时针旋转90度
                        # 转回pdf
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmpf:
                            img.save(tmpf, format="PDF")
                            tmp_pdf_path = tmpf.name
                        img_pdf = fitz.open(tmp_pdf_path)
                        images.append(img_pdf)
                        os.remove(tmp_pdf_path)
                    else:
                        pix = page.get_pixmap(dpi=200)
                        img_pdf = fitz.open()
                        img_pdf.new_page(width=pix.width, height=pix.height)
                        img_pdf[0].insert_image(fitz.Rect(0, 0, pix.width, pix.height), pixmap=pix)
                        images.append(img_pdf)
                    src.close()
                for i in range(0, len(images), n_per_page):
                    page = doc.new_page(width=a4_width, height=a4_height)
                    if n_per_page == 2:
                        w, h = a4_width, a4_height / 2
                        positions = [(0, 0), (0, h)]
                    else:
                        w, h = a4_width / 2, a4_height / 2
                        positions = [(0, 0), (w, 0), (0, h), (w, h)]
                    for j, img_pdf in enumerate(images[i:i+n_per_page]):
                        rect = fitz.Rect(*positions[j], positions[j][0]+w, positions[j][1]+h)
                        page.show_pdf_page(rect, img_pdf, 0)
                doc.save(str(output_path))
            
            # 合并逻辑
            if invoice_paths and int(invoice_merge_mode) in [2, 4]:
                # 1. 先合并BR单、附件、境外票据
                pre_paths = [f[1] for f in files_to_merge if (f[0]["type"] == "brno" or (f[0].get("attach_type") and "附件" in f[0].get("attach_type")) or f[0].get("attach_type") == "境外票据")]
                # 2. 生成发票N合1PDF到临时文件
                invoice_temp_path = str(output_path) + ".invoice.pdf"
                merge_pdfs_nup(invoice_paths, invoice_temp_path, n_per_page=int(invoice_merge_mode))
                # 3. 合并顺序：BR单+附件+境外票据 -> 发票N合1
                temp_path = str(output_path) + ".tmp.pdf"
                merge_order = []
                if pre_paths:
                    merge_order.extend(pre_paths)
                merge_order.append(invoice_temp_path)
                merge_pdfs_with_pikepdf(merge_order, temp_path, failed_files)
                os.rename(temp_path, str(output_path))
                os.remove(invoice_temp_path)
            else:
                # 全部普通合并 - 直接使用files_to_merge的顺序，已经按照业务要求排列
                merge_pdfs_with_pikepdf([f[1] for f in files_to_merge], output_path, failed_files)
            
            relative_path = f"{session_id}/merged/{output_filename}"
            preview_url = f"/sessions/{relative_path}"
            html_content = f"""
                <div class="file-link">
                    <a href="{preview_url}" target="_blank">
                        📄 {output_filename}
                    </a>
                </div>
            """
            success_msg = f"<div class='success'>✅ 合并完成: {output_filename}</div>"
            if failed_files:
                failed_list = "<br>".join(failed_files)
                success_msg += f"<div class='error'>❌ 以下文件处理或合并失败: <br>{failed_list}</div>"
            return [
                gr.update(value=html_content, visible=True),
                gr.update(value=success_msg, visible=True),
            ]
                
            # except Exception as e:
            #     import traceback
            #     traceback.print_exc()
            #     error_msg = f"<div class='error'>❌ 合并过程中发生意外错误: {str(e)}</div>"
            #     return [
            #         gr.update(visible=False),
            #         gr.update(value=error_msg, visible=True),
            #     ]

        async def process_file_for_merge(file: Dict, user_session) -> Optional[str]:
            """处理单个文件用于合并，移除ofd转pdf"""
            user_session.last_accessed = datetime.now()
            file_path = file["path"]
            file_ext = os.path.splitext(file_path)[1].lower()
            output_dir = str(user_session.get_file_dir())
            try:
                if file_ext in ('.png', '.jpg', '.jpeg'):
                    return await asyncio.get_event_loop().run_in_executor(
                        thread_pool, image_to_pdf, file_path, output_dir
                    )
                elif file_ext in ('.doc', '.docx'):
                    return await asyncio.get_event_loop().run_in_executor(
                        thread_pool, word_to_pdf, file_path, output_dir
                    )
                elif file_ext == '.pdf':
                    return file_path
                else:
                    return None
            except Exception as e:
                print(f"处理文件失败 {file['filename']}: {str(e)}")
                return None

        # 定时刷新界面数据
        def refresh_interface(session_state):
            """定时刷新界面数据"""
            session_id = session_state.get('session_id')
            if session_id:
                # 首先更新本地会话的访问时间
                user_session = state_manager.get_session(session_id)
                if user_session:
                    user_session.last_accessed = datetime.now()
                
                # 直接使用本地状态管理器检查会话状态
                if user_session and not user_session.processing:
                    file_results = load_initial_files(session_state)
                    return file_results
                else:
                    # 会话正在处理中，返回处理状态
                    return [
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(value="<div class='info'>⏳ 文件加载中...</div>", visible=True),
                        session_state
                    ]
            else:
                # 没有会话ID，尝试自动连接最新会话
                try:
                    import requests
                    response = requests.get(f"{APP_INTERNAL_BASE_URL}/api/latest_session")
                    if response.status_code == 200:
                        data = response.json()
                        session_id = data["session_id"]
                        session_state = session_state.copy()
                        session_state['session_id'] = session_id
                        
                        print(f"[前端] 自动连接到会话: {session_id[:8]}...")
                        file_results = load_initial_files(session_state)
                        return file_results
                except Exception as e:
                    print(f"[前端] 自动连接会话失败: {e}")
                
                return load_initial_files(session_state)

        def refresh_interface_with_session_id(session_state):
            """定时刷新界面数据，包括会话ID显示和下载进度"""
            print(f"[定时器] 开始刷新界面，当前session_state: {session_state}")
            session_id = session_state.get('session_id')
            if session_id:
                print(f"[定时器] 当前会话ID: {session_id[:8]}...")
                # 首先更新本地会话的访问时间
                user_session = state_manager.get_session(session_id)
                if user_session:
                    user_session.last_accessed = datetime.now()
                    print(f"[定时器] 会话处理状态: {user_session.processing}")
                
                # 直接使用本地状态管理器检查会话状态
                if user_session and not user_session.processing:
                    print(f"[定时器] 会话处理完成，加载文件...")
                    file_results = load_initial_files(session_state)
                    return [session_id] + file_results + [gr.update(visible=False)]  # 隐藏进度条
                else:
                    # 会话正在处理中，获取下载进度
                    print(f"[定时器] 会话正在处理中，获取下载进度...")
                    try:
                        import requests
                        response = requests.get(f"{APP_INTERNAL_BASE_URL}/api/session/{session_id}/download_progress")
                        if response.status_code == 200:
                            progress_data = response.json()
                            progress_info = progress_data.get("progress", {})
                            
                            # 构建进度显示HTML
                            if progress_info["total_files"] > 0:
                                progress_html = f"""
                                <div class='info'>
                                    <h3>📥 文件下载进度</h3>
                                    <div style='margin: 10px 0;'>
                                        <div style='background: #e5e7eb; border-radius: 10px; height: 20px; overflow: hidden;'>
                                            <div style='background: var(--primary); height: 100%; width: {progress_info["progress_percent"]}%; transition: width 0.3s;'></div>
                                        </div>
                                        <div style='margin-top: 5px; font-size: 14px;'>
                                            {progress_info["downloaded_files"]}/{progress_info["total_files"]} 文件 
                                            ({progress_info["progress_percent"]}%) - 
                                            {progress_info["downloaded_size_mb"]}MB / {progress_info["total_size_mb"]}MB
                                        </div>
                                    </div>
                                    <div style='font-size: 12px; color: #6b7280;'>
                                        🔄 正在下载中，请耐心等待... ({progress_data["active_users"]}个用户访问)
                                    </div>
                                </div>
                                """
                            else:
                                progress_html = f"""
                                <div class='info'>
                                    ⏳ 正在准备下载... ({progress_data["active_users"]}个用户访问)
                                </div>
                                """
                            
                            return [
                                session_id,
                                gr.update(choices=[]),
                                gr.update(choices=[]),
                                gr.update(choices=[]),
                                gr.update(choices=[]),
                                gr.update(value="<div class='info'>⏳ 文件下载中...</div>", visible=True),
                                session_state,
                                gr.update(value=progress_html, visible=True)
                            ]
                    except Exception as e:
                        print(f"[定时器] 获取下载进度失败: {e}")
                    
                    # 如果获取进度失败，返回默认状态
                    return [
                        session_id,
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(choices=[]),
                        gr.update(value="<div class='info'>⏳ 文件加载中...</div>", visible=True),
                        session_state,
                        gr.update(visible=False)
                    ]
            else:
                print(f"[定时器] 没有会话ID，尝试自动连接...")
                # 没有会话ID，尝试自动连接最新会话
                try:
                    import requests
                    response = requests.get(f"{APP_INTERNAL_BASE_URL}/api/latest_session")
                    if response.status_code == 200:
                        data = response.json()
                        session_id = data["session_id"]
                        session_state = session_state.copy()
                        session_state['session_id'] = session_id
                        
                        print(f"[前端] 自动连接到会话: {session_id[:8]}...")
                        file_results = load_initial_files(session_state)
                        return [session_id] + file_results + [gr.update(visible=False)]
                    else:
                        print(f"[定时器] API返回状态码: {response.status_code}")
                except Exception as e:
                    print(f"[前端] 自动连接会话失败: {e}")
                
                return [
                    "未连接",
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='warning'>⚠️ 请先连接会话或通过API设置GUID</div>", visible=True),
                    session_state,
                    gr.update(visible=False)
                ]
        
        # 初始化会话（兼容API调用）
        def init_session(session_state):
            """初始化会话，自动尝试连接最新会话"""
            # 如果session_state中已有session_id，直接使用
            if session_state.get('session_id'):
                return load_initial_files(session_state)
            
            # 尝试自动连接最新会话
            try:
                import requests
                response = requests.get(f"{APP_INTERNAL_BASE_URL}/api/latest_session")
                if response.status_code == 200:
                    data = response.json()
                    session_id = data["session_id"]
                    session_state = session_state.copy()
                    session_state['session_id'] = session_id
                    
                    print(f"[前端] 自动连接到会话: {session_id[:8]}...")
                    return load_initial_files(session_state)
            except Exception as e:
                print(f"[前端] 自动连接会话失败: {e}")
            
            # 无法自动连接，返回等待状态
            return [
                gr.update(choices=[]),
                gr.update(choices=[]),
                gr.update(choices=[]),
                gr.update(choices=[]),
                gr.update(value="<div class='warning'>⚠️ 请先连接会话或通过API设置GUID</div>", visible=True),
                session_state
            ]
        
        # 连接最新会话按钮事件
        def connect_and_update(session_state):
            """连接最新会话并更新界面"""
            session_id = connect_latest_session()
            if session_id:
                session_state = session_state.copy()
                session_state['session_id'] = session_id
                file_results = load_initial_files(session_state)
                return [session_id] + file_results
            else:
                return [
                    "未连接",
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='error'>⚠️ 未找到活跃会话</div>", visible=True),
                    session_state
                ]

        connect_btn.click(
            fn=connect_and_update,
            inputs=[session_state],
            outputs=[session_id_display, brno_selector, invoice_selector, bill_selector, overseas_selector, status_display, session_state]
        )

        # 自动加载文件
        demo.load(
            fn=init_session,
            inputs=[session_state],
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector, status_display, session_state],
            api_name=False
        )
        
        # 添加定时器，每3秒刷新一次界面
        timer = gr.Timer(3)
        timer.tick(
            fn=refresh_interface_with_session_id,
            inputs=[session_state],
            outputs=[session_id_display, brno_selector, invoice_selector, bill_selector, overseas_selector, status_display, session_state, progress_display]
        )

        # 全局按钮事件
        global_select_all.click(
            fn=select_all_global,
            inputs=[session_state],
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector]
        )

        global_clear_all.click(
            fn=clear_all_global,
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector]
        )

        # 类别选择按钮事件
        invoice_select_all.click(
            fn=lambda session_state: select_all_by_type("发票", session_state),
            inputs=[session_state],
            outputs=[invoice_selector]
        )
        
        bill_select_all.click(
            fn=lambda session_state: select_all_by_type("附件", session_state),
            inputs=[session_state],
            outputs=[bill_selector]
        )
        
        overseas_select_all.click(
            fn=lambda session_state: select_all_by_type("境外票据", session_state),
            inputs=[session_state],
            outputs=[overseas_selector]
        )

        # 清空按钮事件
        for clear_btn, selector in [
            (invoice_clear_all, invoice_selector),
            (bill_clear_all, bill_selector),
            (overseas_clear_all, overseas_selector)
        ]:
            clear_btn.click(fn=lambda: gr.update(value=[]), outputs=[selector])

        # 选择变化事件
        for selector in [brno_selector, invoice_selector, bill_selector, overseas_selector]:
            selector.change(
                fn=update_merge_order,
                inputs=[brno_selector, invoice_selector, bill_selector, overseas_selector, merge_order_state],
                outputs=[merge_order_state]
            )

        def clear_all_selectors():
            """Clears all file selection checkboxes after merging."""
            return gr.update(value=[]), gr.update(value=[]), gr.update(value=[]), gr.update(value=[])

        def prepare_for_merge():
            """Clears previous results and makes the status component visible to show progress."""
            return gr.update(visible=False), gr.update(value="", visible=True)

        # 文件顺序管理按钮事件
        update_order_btn.click(
            fn=update_file_order,
            inputs=[brno_selector, invoice_selector, bill_selector, overseas_selector, session_state],
            outputs=[file_order_display, file_order_state]
        )
        
        clear_order_btn.click(
            fn=clear_file_order,
            outputs=[file_order_display, file_order_state]
        )

        # 合并按钮事件
        merge_event = merge_btn.click(
            fn=prepare_for_merge,
            inputs=None,
            outputs=[file_link, status_label]
        ).then(
            fn=merge_files_async,
            inputs=[merge_order_state, session_state, invoice_merge_mode, file_order_display],
            outputs=[file_link, status_label]
        ).then(
            fn=clear_all_selectors,
            inputs=None,
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector]
        )
        
    return demo

def deduplicate_files(files: List[Dict]) -> List[Dict]:
    seen = set()
    unique_files = []
    for f in files:
        key = (f["filename"], os.path.getsize(f["path"]) if os.path.exists(f["path"]) else 0)
        if key not in seen:
            unique_files.append(f)
            seen.add(key)
    return unique_files

gradio_app = create_interface()
app = mount_gradio_app(app, gradio_app, path="/app")

async def periodic_cleanup_sessions(interval_seconds: int = 300, max_age_seconds: int = 3600):
    """
    Periodically cleans up old sessions.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        print("[后台清理] 开始清理过期会话...")
        now = datetime.now()
        expired_sessions = []
        with state_manager.state_lock:
            for session_id, session in state_manager.user_sessions.items():
                if now - session.last_accessed > timedelta(seconds=max_age_seconds):
                    expired_sessions.append(session_id)
        
        if expired_sessions:
            print(f"[后台清理] 发现 {len(expired_sessions)} 个过期会话，正在清理...")
            for session_id in expired_sessions:
                # cleanup_session already handles locking
                state_manager.cleanup_session(session_id)
        else:
            print("[后台清理] 没有发现过期会话。")


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(periodic_cleanup_sessions())

@app.get("/api/status")
async def get_status():
    """获取系统状态"""
    stats = state_manager.get_stats()
    return JSONResponse(
        content={
            "status": "success",
            "stats": stats
        }
    )

@app.get("/api/latest_session")
async def get_latest_session():
    """获取最新创建的会话（用于前端界面连接）"""
    with state_manager.state_lock:
        if not state_manager.user_sessions:
            return JSONResponse(
                content={"status": "error", "message": "没有活跃会话"},
                status_code=404
            )
        
        # 返回最新创建的会话
        latest_session_id = max(
            state_manager.user_sessions.keys(),
            key=lambda sid: state_manager.user_sessions[sid].created_at
        )
        user_session = state_manager.user_sessions[latest_session_id]
        
        return JSONResponse(
            content={
                "status": "success",
                "session_id": latest_session_id,
                "guid": user_session.guid,
                "processing": user_session.processing,
                "file_count": len(user_session.files),
                "created_at": user_session.created_at.isoformat()
            }
        )

@app.get("/api/session/{session_id}")
async def get_session_info(session_id: str):
    """获取会话信息"""
    user_session = state_manager.get_session(session_id)
    if not user_session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 刷新活跃时间
    user_session.last_accessed = datetime.now()
    
    return JSONResponse(
        content={
            "status": "success",
            "session_id": session_id,
            "guid": user_session.guid,
            "processing": user_session.processing,
            "file_count": len(user_session.files),
            "brno": user_session.brno
        }
    )

@app.get("/api/session/{session_id}/files")
async def get_session_files(session_id: str):
    """获取会话的文件列表"""
    user_session = state_manager.get_session(session_id)
    if not user_session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 刷新活跃时间
    user_session.last_accessed = datetime.now()
    
    files = user_session.get_files()
    
    # 为每个文件添加访问URL
    for file_info in files:
        file_path = Path(file_info["path"])
        # 计算相对于会话目录的路径
        try:
            relative_path = file_path.relative_to(user_session.session_dir)
            file_info["url"] = f"/sessions/{session_id}/{relative_path}"
        except ValueError:
            # 如果文件不在会话目录中（例如，来自旧的逻辑），则跳过URL生成
            file_info["url"] = None

    return JSONResponse(
        content={
            "status": "success",
            "session_id": session_id,
            "files": files
        }
    )

@app.get("/api/session/{session_id}/merged")
async def get_session_merged_files(session_id: str):
    """获取会话的合并文件列表"""
    user_session = state_manager.get_session(session_id)
    if not user_session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    merged_dir = user_session.get_merge_dir()
    merged_files = []
    
    if merged_dir.exists():
        for file_path in merged_dir.glob("*.pdf"):
            file_info = {
                "filename": file_path.name,
                "url": f"/sessions/{session_id}/merged/{file_path.name}",
                "size": file_path.stat().st_size,
                "created_time": datetime.fromtimestamp(file_path.stat().st_ctime).isoformat()
            }
            merged_files.append(file_info)
    
    return JSONResponse(
        content={
            "status": "success",
            "session_id": session_id,
            "merged_files": merged_files
        }
    )


@app.get("/api/session/{session_id}/download_progress")
async def get_download_progress(session_id: str):
    """获取会话的下载进度信息"""
    user_session = state_manager.get_session(session_id)
    if not user_session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    files = user_session.get_files()
    total_files = len(files)
    downloaded_files = 0
    total_size = 0
    downloaded_size = 0
    
    for file_info in files:
        if os.path.exists(file_info["path"]):
            downloaded_files += 1
            file_size = os.path.getsize(file_info["path"])
            total_size += file_size
            downloaded_size += file_size
    
    # 计算进度百分比
    progress_percent = (downloaded_files / total_files * 100) if total_files > 0 else 0
    
    # 获取GUID级别的处理状态
    guid_data = state_manager.get_guid_data(guid=user_session.guid)
    active_users = len(guid_data.get("users", set())) if guid_data else 1
    
    return JSONResponse(
        content={
            "status": "success",
            "session_id": session_id,
            "progress": {
                "total_files": total_files,
                "downloaded_files": downloaded_files,
                "progress_percent": round(progress_percent, 1),
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "downloaded_size_mb": round(downloaded_size / (1024 * 1024), 2)
            },
            "processing": user_session.processing,
            "active_users": active_users,
            "guid": user_session.guid
        }
    )

@app.get("/")
async def root():
    return RedirectResponse("/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)