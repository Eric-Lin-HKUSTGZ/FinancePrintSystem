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

# 配置参数
AUTH_USER = "brgpt"
AUTH_PASS = "jiyMBV432-HAS98"
BASE_URL = "https://pbms.hkust-gz.edu.cn"
BASE_STATIC_DIR = "./test_file"
TEMP_DIR = tempfile.gettempdir()
GUID_FILE_DIR = "./test_file/guid_files"

# 创建基础目录
os.makedirs(BASE_STATIC_DIR, exist_ok=True)
os.makedirs(GUID_FILE_DIR, exist_ok=True)

app = FastAPI()

# 创建目录
os.makedirs(os.path.join(BASE_STATIC_DIR, "sessions"), exist_ok=True)

# 用户会话管理类（参考main.py的设计）
class UserSession:
    def __init__(self, session_id: str, guid: str):
        self.session_id = session_id
        self.guid = guid
        # 创建基于session_id的目录结构
        self.base_dir = Path("./test_file2/sessions")
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
    # 获取或创建该guid的锁
    if guid not in state_manager.guid_locks:
        state_manager.guid_locks[guid] = asyncio.Lock()
    async with state_manager.guid_locks[guid]:
        # 检查是否已处理过且有文件，直接复用
        with state_manager.state_lock:
            guid_data = state_manager.guid_files.get(guid)
            all_session_ids = state_manager.guid_sessions.get(guid, set())
            brno = guid_data.get("brno", "") if guid_data else ""
            files = guid_data["files"] if guid_data else []
            should_distribute = guid_data and not guid_data["processing"] and files

        if should_distribute:
            print(f"[DEBUG] GUID {guid[:8]}... 已有缓存，分发到所有session")
            if all_session_ids:
                source_session_id = next(iter(all_session_ids))
                await copy_files_to_sessions(files, all_session_ids, source_session_id)
                # 更新所有session的files
                state_manager.update_guid_data(guid, files, brno)
            return
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
            # 并发下载文件
            download_tasks = []
            for file_type, g, _ in brno_items:
                task = asyncio.create_task(download_file_async(file_type, g, user_session=user_session))
                download_tasks.append(task)
            for item in file_items:
                file_type, g, name, attachtype = item
                task = asyncio.create_task(download_file_async(file_type, g, name, attachtype, user_session=user_session))
                download_tasks.append(task)
            # 等待所有下载完成，加超时
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*download_tasks, return_exceptions=True),
                    timeout=120
                )
            except asyncio.TimeoutError:
                print(f"[TIMEOUT] GUID {guid[:8]}... 文件下载超时")
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
    return await asyncio.get_event_loop().run_in_executor(
        thread_pool, download_file, file_type, guid, decoded_name, attachtype, user_session
    )

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
app.mount("/sessions", CustomStaticFiles(directory="./test_file2/sessions"), name="sessions")
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

    # 如果已存在，直接返回
    if os.path.exists(real_file_path):
        main_file_info = {
            "guid": guid,
            "filename": filename,
            "path": real_file_path,
            "type": file_type
        }
        if attachtype:
            main_file_info["attach_type"] = attachtype
        return main_file_info, []

    # 否则下载
    endpoint = "br/sysdownload" if file_type == "brno" else "file/download"
    url = f"{BASE_URL}/{endpoint}?g={guid}"
    auth = HTTPBasicAuth(AUTH_USER, AUTH_PASS)
    headers = {"Content-Type": "application/pdf; charset=utf-8"}

    try:
        response = requests.post(
            url,
            auth=auth,
            headers=headers,
            stream=True,
            timeout=30
        )
        if response.status_code != 200:
            print(f"[下载失败] {url} status={response.status_code} filename={decoded_name} attachtype={attachtype} response={response.text}")
            return None, []

        with open(real_file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        main_file_info = {
            "guid": guid,
            "filename": filename,
            "path": real_file_path,
            "type": file_type
        }
        if attachtype:
            main_file_info["attach_type"] = attachtype
        return main_file_info, []
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
    """

    js_func = """
    function refresh() {
        const url = new URL(window.location);
        if (url.searchParams.get('__theme') !== 'light') {
            url.searchParams.set('__theme', 'light');
            window.location.href = url.href;
        }
    }
    
    // 页面关闭时清理会话的机制（暂时禁用，使用后台定时清理）
    // 页面离开检测可能过于敏感，导致用户正常使用时会话被误删
    // 改为依赖后台的定时清理机制来处理过期会话
    """

    with gr.Blocks(title="PBMS文件合并工具", css=custom_css, js=js_func) as demo:
        with gr.Column(elem_classes="guide-box"):
            gr.Markdown("""
            ## 🚀 操作指南
            1. 通过API设置GUID（POST `/api/set_guid`）或手动输入会话ID
            2. 选择需要合并的文件类型
            3. 点击 **开始合并** 生成PDF文件
            4. 点击生成的文档名称即可预览
            """)

        # 会话管理区域
        with gr.Row():
            with gr.Column(scale=2):
                session_input = gr.Textbox(
                    label="会话ID (Session ID)",
                    placeholder="手动输入会话ID或点击'连接最新会话'自动获取",
                    interactive=True
                )
            with gr.Column(scale=1):
                connect_btn = gr.Button("连接最新会话", variant="secondary")
                session_info = gr.Textbox(
                    label="会话状态",
                    value="未连接",
                    interactive=False
                )

        # 会话状态管理（参考main.py的设计）
        session_state = gr.State({})
        status_display = gr.HTML(visible=False)

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
                    gr.Markdown("### 发票文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        invoice_select_all = gr.Button("全选", size="sm")
                        invoice_clear_all = gr.Button("清空", size="sm")
                invoice_selector = gr.CheckboxGroup(label="选择发票文件", elem_classes="checkbox-group")
            
            with gr.Column(elem_classes="selector-card"):
                with gr.Row(elem_classes="selector-header"):
                    gr.Markdown("### 附件文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        bill_select_all = gr.Button("全选", size="sm")
                        bill_clear_all = gr.Button("清空", size="sm")
                bill_selector = gr.CheckboxGroup(label="选择附件文件", elem_classes="checkbox-group")
            
            with gr.Column(elem_classes="selector-card"):
                with gr.Row(elem_classes="selector-header"):
                    gr.Markdown("### 境外票据文件", elem_classes="section-title")
                    with gr.Row(elem_classes="btn-group"):
                        overseas_select_all = gr.Button("全选", size="sm")
                        overseas_clear_all = gr.Button("清空", size="sm")
                overseas_selector = gr.CheckboxGroup(label="选择境外票据文件", elem_classes="checkbox-group")
        
        with gr.Row():
            merge_btn = gr.Button("✨ 开始合并", variant="primary", scale=0)
        
        with gr.Column():
            file_link = gr.HTML(visible=False)
            status_label = gr.HTML(visible=False)

        merge_order_state = gr.State([])

        def connect_latest_session():
            """连接最新的会话"""
            try:
                import requests
                response = requests.get("http://local/api/latest_session")
                if response.status_code == 200:
                    data = response.json()
                    session_id = data["session_id"]
                    guid = data["guid"]
                    processing = data["processing"]
                    file_count = data["file_count"]
                    
                    status_text = f"已连接会话: {session_id[:8]}...\nGUID: {guid[:8]}...\n文件数: {file_count}\n状态: {'处理中' if processing else '就绪'}"
                    
                    return session_id, status_text
                else:
                    return "", "⚠️ 没有找到活跃会话"
            except Exception as e:
                return "", f"❌ 连接失败: {str(e)}"

        def set_session_from_input(session_id_input, session_state):
            """从输入框设置会话ID"""
            if not session_id_input.strip():
                return session_state, "❌ 请输入会话ID"
            
            session_id = session_id_input.strip()
            try:
                import requests
                response = requests.get(f"http://localhost/app/session/{session_id}")
                if response.status_code == 200:
                    data = response.json()
                    session_state = session_state.copy()
                    session_state['session_id'] = session_id
                    
                    guid = data["guid"]
                    processing = data["processing"]
                    file_count = data["file_count"]
                    
                    status_text = f"已连接会话: {session_id[:8]}...\nGUID: {guid[:8]}...\n文件数: {file_count}\n状态: {'处理中' if processing else '就绪'}"
                    
                    return session_state, status_text
                else:
                    return session_state, "❌ 会话不存在"
            except Exception as e:
                return session_state, f"❌ 连接失败: {str(e)}"

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
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value=f"<div class='info'>⏳ 文件加载中... (共{active_users}个用户访问)</div>", visible=True),
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
                elif "附件" in f.get("attach_type", ""):
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
            selected = brno + invoice + bill + overseas
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
                elif attach_type == "附件" and "附件" in f.get("attach_type", ""):
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
            invoice_guids = []
            bill_guids = []
            overseas_guids = []

            for f in files:
                file_ext = os.path.splitext(f["filename"])[1].lower()
                if file_ext not in allowed_extensions:
                    continue
                if f["type"] == "brno":
                    brno_guids.append(f["guid"])
                elif f.get("attach_type") == "发票":
                    invoice_guids.append(f["guid"])
                elif "附件" in f.get("attach_type", ""):
                    bill_guids.append(f["guid"])
                elif f.get("attach_type") == "境外票据":
                    overseas_guids.append(f["guid"])

            return [
                gr.update(value=brno_guids),
                gr.update(value=invoice_guids),
                gr.update(value=bill_guids),
                gr.update(value=overseas_guids)
            ]

        def clear_all_global():
            """清除所有选择"""
            return [gr.update(value=[])]*4

        async def merge_files_async(selected_guids: list, session_state, progress: gr.Progress = gr.Progress()):
            session_id = session_state.get('session_id')
            if not session_id:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 请先通过API设置GUID</div>", visible=True),
                    gr.update(), gr.update(), gr.update()
                ]
            
            user_session = state_manager.get_session(session_id)
            if not user_session:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 会话不存在</div>", visible=True),
                    gr.update(), gr.update(), gr.update()
                ]
            
            if not selected_guids:
                return [
                    gr.update(visible=False),
                    gr.update(value="<div class='error'>❌ 请至少选择一个文件</div>", visible=True),
                    gr.update(), gr.update(), gr.update()
                ]
            
            try:
                user_session.last_accessed = datetime.now()
                conversion_tasks = []
                failed_files = []
                total_files = len(selected_guids)
                progress(0, desc="正在准备文件...")
                files = user_session.get_files()
                for index, guid in enumerate(selected_guids, 1):
                    file = next((f for f in files if f["guid"] == guid), None)
                    if not file:
                        print(f"[合并] 用户会话 {session_id[:8]}..., 找不到文件GUID {guid}")
                        continue
                    task = asyncio.create_task(process_file_for_merge(file, user_session))
                    conversion_tasks.append((task, file["filename"]))
                    progress(index/total_files*0.5, desc=f"准备文件 {index}/{total_files}")
                converted_files = []
                for task, filename in conversion_tasks:
                    try:
                        result = await task
                        if result:
                            converted_files.append(result)
                    except Exception as e:
                        print(f"[合并] 会话 {session_id[:8]}... 处理文件 {filename} 失败: {str(e)}")
                        failed_files.append(filename)
                if not converted_files:
                    return [
                        gr.update(visible=False),
                        gr.update(value="<div class='error'>❌ 没有可合并的文件</div>", visible=True),
                        gr.update(), gr.update(), gr.update()
                    ]
                
                progress(0.7, desc="正在合并PDF...")
                
                valid_files = []
                missing_files = []
                for file_path in converted_files:
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                        valid_files.append(file_path)
                    else:
                        missing_files.append(os.path.basename(file_path))
                
                # 合并PDF - 使用用户专用目录
                merger = PdfMerger()
                brno_number = user_session.brno
                output_filename = f"{brno_number}.pdf" if brno_number else f"merged_{uuid.uuid4()}.pdf"
                merge_dir = user_session.get_merge_dir()
                output_path = merge_dir / output_filename.replace("/", "_")
                # 防御性检查：如果输出文件名与任何待合并文件路径相同，则更换输出文件名
                valid_file_paths = set(os.path.abspath(f) for f in valid_files)
                while os.path.abspath(str(output_path)) in valid_file_paths:
                    output_filename = f"merged_{uuid.uuid4()}.pdf"
                    output_path = merge_dir / output_filename
                
                for file_path in valid_files:
                    try:
                        merger.append(file_path)
                    except PdfReadError as e:
                        failed_files.append(os.path.basename(file_path))
                
                merger.write(str(output_path))
                merger.close()
                
                progress(1.0, desc="合并完成")
                
                # 生成预览链接 - 直接使用会话目录中的文件
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
                    success_msg += f"<div class='error'>❌ 以下文件合并失败: <br>{failed_list}</div>"
                
                print("用户选择的guid:", selected_guids)
                print("后端匹配到的文件:", [(f['guid'], f['filename'], f['path']) for f in files])
                print("实际待合并文件:", converted_files)
                
                return [
                    gr.update(value=html_content, visible=True),
                    gr.update(value=success_msg, visible=True),
                    gr.update(value=[]),
                    gr.update(value=[]),
                    gr.update(value=[])
                ]
                
            except Exception as e:
                error_msg = f"<div class='error'>❌ 合并失败: {str(e)}</div>"
                return [
                    gr.update(visible=False),
                    gr.update(value=error_msg, visible=True),
                    gr.update(), gr.update(), gr.update()
                ]

        async def process_file_for_merge(file: Dict, user_session) -> Optional[str]:
            """处理单个文件用于合并"""
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
                
                # 更新会话信息
                try:
                    import requests
                    response = requests.get(f"http://10.120.20.213:15198/api/session/{session_id}")
                    if response.status_code == 200:
                        data = response.json()
                        guid = data["guid"]
                        processing = data["processing"]
                        file_count = data["file_count"]
                        
                        status_text = f"已连接会话: {session_id[:8]}...\nGUID: {guid[:8]}...\n文件数: {file_count}\n状态: {'处理中' if processing else '就绪'}"
                        
                        file_results = load_initial_files(session_state)
                        return [status_text] + file_results
                    else:
                        # 会话不存在，清除状态
                        session_state = {}
                        return ["❌ 会话已失效"] + load_initial_files(session_state)
                except Exception as e:
                    return [f"❌ 刷新失败: {str(e)}"] + load_initial_files(session_state)
            else:
                return ["未连接"] + load_initial_files(session_state)
        
        # 初始化会话（兼容API调用）
        def init_session(session_state):
            """初始化会话，自动尝试连接最新会话"""
            # 如果session_state中已有session_id，直接使用
            if session_state.get('session_id'):
                return load_initial_files(session_state)
            
            # 尝试自动连接最新会话
            try:
                import requests
                response = requests.get("http://10.120.20.213:15198/api/latest_session")
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
            session_id, status_text = connect_latest_session()
            if session_id:
                session_state = session_state.copy()
                session_state['session_id'] = session_id
                file_results = load_initial_files(session_state)
                return [session_id, status_text, session_state] + file_results[:-1]  # 除了最后的session_state
            else:
                return [
                    "", status_text, session_state,
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='warning'>⚠️ 无法连接会话</div>", visible=True)
                ]

        connect_btn.click(
            fn=connect_and_update,
            inputs=[session_state],
            outputs=[session_input, session_info, session_state, brno_selector, invoice_selector, bill_selector, overseas_selector, status_display]
        )

        # 手动输入会话ID事件
        def manual_connect(session_id_input, session_state):
            """手动连接会话并更新界面"""
            session_state_new, status_text = set_session_from_input(session_id_input, session_state)
            if session_state_new.get('session_id'):
                file_results = load_initial_files(session_state_new)
                return [status_text, session_state_new] + file_results[:-1]  # 除了最后的session_state
            else:
                return [
                    status_text, session_state,
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value="<div class='warning'>⚠️ 无法连接会话</div>", visible=True)
                ]

        session_input.submit(
            fn=manual_connect,
            inputs=[session_input, session_state],
            outputs=[session_info, session_state, brno_selector, invoice_selector, bill_selector, overseas_selector, status_display]
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
            fn=refresh_interface,
            inputs=[session_state],
            outputs=[session_info, brno_selector, invoice_selector, bill_selector, overseas_selector, status_display, session_state]
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

        # 合并按钮事件
        merge_btn.click(
            fn=merge_files_async,
            inputs=[merge_order_state, session_state],
            outputs=[file_link, status_label, invoice_selector, bill_selector, overseas_selector]
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
        relative_path = file_path.relative_to(user_session.session_dir)
        file_info["url"] = f"/sessions/{session_id}/{relative_path}"
    
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

@app.get("/")
async def root():
    return RedirectResponse("/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9998)