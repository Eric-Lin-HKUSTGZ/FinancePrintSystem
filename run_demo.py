from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr
from gradio.routes import mount_gradio_app
import os
import uuid
from typing import Dict, List, Tuple
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

# 配置参数
AUTH_USER = "brgpt"
AUTH_PASS = "jiyMBV432-HAS98"
BASE_URL = "https://pbms.hkust-gz.edu.cn"
STATIC_DIR = "./test_file/static"
BRNO_DIR = "./test_file/brno"
FILE_DIR = "./test_file/file"
TEMP_DIR = tempfile.gettempdir()

# 创建目录
os.makedirs(BRNO_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "merged"), exist_ok=True)

app = FastAPI()

# 全局存储当前GUID
current_guid = None
pbms_files: Dict[str, Dict] = {}

# 定义数据模型
class GuidRequest(BaseModel):
    guid: str

@app.post("/api/set_guid")
async def set_guid(request: GuidRequest):
    """设置当前GUID并自动加载文件"""
    global current_guid
    current_guid = request.guid
    
    try:
        clean_directories()
        if current_guid in pbms_files:
            del pbms_files[current_guid]

        # 处理GUID并下载文件
        brno_number, brno_items, file_items = process_guids(current_guid)
        pbms_files[current_guid] = {"brno": brno_number, "files": []}

        # 处理BRNO文件
        for file_type, g, _ in brno_items:
            file_info, extracted_files, error = download_file(file_type, g)
            if not error:
                pbms_files[current_guid]["files"].append(file_info)
                for extracted_file in extracted_files:
                    pbms_files[current_guid]["files"].append(extracted_file)
        
        # 处理普通文件
        for item in file_items:
            file_type, g, name, attachtype = item
            file_info, extracted_files, error = download_file(file_type, g, decoded_name=name, attachtype=attachtype)
            if not error:
                if file_info['filename'].lower().endswith(('.zip', '.rar')):
                    for extracted_file in extracted_files:
                        extracted_file['attach_type'] = attachtype
                        pbms_files[current_guid]["files"].append(extracted_file)
                else:
                    file_info["attach_type"] = attachtype
                    pbms_files[current_guid]["files"].append(file_info)
                    for extracted_file in extracted_files:
                        pbms_files[current_guid]["files"].append(extracted_file)

        return JSONResponse(
            content={
                "status": "success",
                "message": f"GUID已更新为 {current_guid}，文件加载成功",
                "guid": current_guid
            }
        )
    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": f"文件加载失败: {str(e)}",
                "guid": current_guid
            },
            status_code=500
        )

class CustomStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except RuntimeError:
            response = await super().get_response("index.html", scope)
        
        if isinstance(response, FileResponse):
            file_ext = os.path.splitext(response.path)[1].lower()
            if file_ext == ".pdf":
                response.headers["Content-Type"] = "application/pdf"
                if "Content-Disposition" in response.headers:
                    del response.headers["Content-Disposition"]
        return response

app.mount("/static", CustomStaticFiles(directory=STATIC_DIR), name="static")

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

def download_file(file_type: str, guid: str, decoded_name: str = None, attachtype: str = None) -> Tuple[Dict, List[Dict], str]:
    endpoint = "br/sysdownload" if file_type == "brno" else "file/download"
    url = f"{BASE_URL}/{endpoint}?g={guid}"
    save_dir = BRNO_DIR if file_type == "brno" else FILE_DIR
    os.makedirs(save_dir, exist_ok=True)
    
    auth = HTTPBasicAuth(AUTH_USER, AUTH_PASS)
    try:
        response = requests.post(
            url,
            auth=auth,
            headers={"Content-Type": "application/json; charset=utf-8"},
            stream=True,
            timeout=30
        )
        
        if response.status_code != 200:
            return {}, [], f"HTTP {response.status_code}"
        
        filename = decoded_name or ""
        if not filename:
            content_disposition = response.headers.get('Content-Disposition', '')
            if 'filename=' in content_disposition:
                filename = content_disposition.split('filename=')[-1].strip('"')
                try:
                    filename = filename.encode('latin-1').decode('utf-8')
                except:
                    pass
            else:
                ext = "pdf" if file_type == "brno" else "bin"
                filename = f"{file_type}_{guid}.{ext}"

        filename = filename.replace('/', '_').replace('\\', '_')
        
        base_name, ext = os.path.splitext(filename)
        counter = 1
        final_name = filename
        while os.path.exists(os.path.join(save_dir, final_name)):
            final_name = f"{base_name}_{counter}{ext}"
            counter += 1
        
        save_path = os.path.join(save_dir, final_name)
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        main_file_info = {
            "guid": guid,
            "filename": final_name,
            "path": save_path,
            "type": file_type
        }

        extracted_files = []
        
        if file_type == "file" and attachtype:
            file_ext = os.path.splitext(save_path)[1].lower()
            if file_ext in ['.zip', '.rar']:
                extract_dir = os.path.join(save_dir, attachtype)
                os.makedirs(extract_dir, exist_ok=True)
                
                try:
                    if file_ext == '.zip':
                        with zipfile.ZipFile(save_path, 'r') as zip_ref:
                            zip_ref.extractall(extract_dir)
                    elif file_ext == '.rar':
                        with rarfile.RarFile(save_path, 'r') as rar_ref:
                            rar_ref.extractall(extract_dir)

                    for root, dirs, files in os.walk(extract_dir):
                        for file in files:
                            src_path = os.path.join(root, file)
                            relative_path = os.path.relpath(src_path, extract_dir)
                            if os.path.dirname(relative_path) != '.':
                                dest_path = os.path.join(extract_dir, file)
                                base, ext = os.path.splitext(file)
                                counter = 1
                                while os.path.exists(dest_path):
                                    dest_path = os.path.join(extract_dir, f"{base}_{counter}{ext}")
                                    counter += 1
                                shutil.move(src_path, dest_path)
                                src_path = dest_path

                            final_name = os.path.basename(src_path)
                            base, ext = os.path.splitext(final_name)
                            counter = 1
                            while os.path.exists(os.path.join(extract_dir, final_name)):
                                final_name = f"{base}_{counter}{ext}"
                                counter += 1
                            final_path = os.path.join(extract_dir, final_name)
                            if src_path != final_path:
                                shutil.move(src_path, final_path)

                            extracted_files.append({
                                "guid": str(uuid.uuid4()),
                                "filename": final_name,
                                "path": final_path,
                                "type": "file",
                                "attach_type": attachtype
                            })
                except Exception as e:
                    print(f"解压失败: {str(e)}")

        return main_file_info, extracted_files, None
    except Exception as e:
        return {}, [], str(e)

def image_to_pdf(image_path: str) -> str:
    """将图片转换为PDF文件，处理无效的Exif方向信息"""
    pdf_path = os.path.splitext(image_path)[0] + ".pdf"
    
    try:
        # 使用Rotation.ifvalid处理无效的旋转值
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_path, rotation=Rotation.ifvalid))
    except Exception as e:
        # 如果转换失败，尝试使用PIL进行转换
        print(f"img2pdf转换失败，使用PIL重试: {str(e)}")
        try:
            img = Image.open(image_path)
            img.save(pdf_path, "PDF", resolution=100.0)
        except Exception as pil_e:
            # 如果两种方法都失败，抛出异常
            raise RuntimeError(f"图片转PDF失败: {str(pil_e)}") from pil_e
    
    return pdf_path

def word_to_pdf(word_path: str) -> str:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_name = os.path.splitext(os.path.basename(word_path))[0] + ".pdf"
            pdf_path = os.path.join(temp_dir, pdf_name)
            
            cmd = [
                'soffice',
                '--headless',
                '--convert-to', 'pdf',
                '--outdir', temp_dir,
                word_path
            ]
            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            if not os.path.exists(pdf_path):
                generated_files = os.listdir(temp_dir)
                matching_files = [f for f in generated_files if f.lower().endswith('.pdf')]
                
                if not matching_files:
                    raise RuntimeError(f"转换失败，未生成PDF文件。输出目录内容：{generated_files}")
                
                actual_pdf_path = os.path.join(temp_dir, matching_files[0])
                shutil.copy(actual_pdf_path, pdf_path)
            
            final_dir = os.path.join(FILE_DIR, "temp_pdfs")
            os.makedirs(final_dir, exist_ok=True)
            final_path = os.path.join(final_dir, os.path.basename(pdf_path))
            
            counter = 1
            while os.path.exists(final_path):
                base_name, ext = os.path.splitext(os.path.basename(pdf_path))
                final_path = os.path.join(final_dir, f"{base_name}_{counter}{ext}")
                counter += 1
            
            shutil.move(pdf_path, final_path)
            return final_path
            
    except subprocess.CalledProcessError as e:
        error_msg = f"LibreOffice错误: {e.stderr}" if e.stderr else f"错误代码: {e.returncode}"
        raise RuntimeError(f"Word转PDF失败: {error_msg}") from e
    except Exception as e:
        raise RuntimeError(f"转换错误: {str(e)}") from e


def clean_directories():
    dirs_to_clean = [BRNO_DIR, FILE_DIR, os.path.join(STATIC_DIR, "merged")]
    for dir_path in dirs_to_clean:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)
        os.makedirs(dir_path, exist_ok=True)
    
    temp_pdf_dir = os.path.join(FILE_DIR, "temp_pdfs")
    if os.path.exists(temp_pdf_dir):
        shutil.rmtree(temp_pdf_dir)

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
    """

    with gr.Blocks(title="PBMS文件合并工具", css=custom_css, js=js_func) as demo:
        with gr.Column(elem_classes="guide-box"):
            gr.Markdown("""
            ## 🚀 操作指南
            1. 通过API设置GUID（POST `/api/set_guid`）
            2. 选择需要合并的文件类型
            3. 点击 **开始合并** 生成PDF文件
            4. 点击生成的文档名称即可预览
            """)

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
            
            # 添加新的境外票据类别展示栏
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

        def load_initial_files():
            global current_guid
            if not current_guid or current_guid not in pbms_files:
                return [gr.update(choices=[])]*4 + [gr.update(visible=False)]
            
            allowed_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
            brno_files = []
            invoice_files = []
            bill_files = []
            overseas_files = []  # 新增：境外票据文件列表
            
            for f in pbms_files[current_guid]["files"]:
                file_ext = os.path.splitext(f["filename"])[1].lower()
                if file_ext not in allowed_extensions:
                    continue
                if f["type"] == "brno":
                    brno_files.append(f)
                elif f.get("attach_type") == "发票":
                    invoice_files.append(f)
                # 修改：只要attachtype包含"附件"两字，就归类到附件文件类别
                elif "附件" in f.get("attach_type", ""):
                    bill_files.append(f)
                elif f.get("attach_type") == "境外票据":  # 新增：处理境外票据类别
                    overseas_files.append(f)
            
            return [
                gr.update(choices=[(f["filename"], f["guid"]) for f in brno_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in invoice_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in bill_files]),
                gr.update(choices=[(f["filename"], f["guid"]) for f in overseas_files]),  # 新增：境外票据选择框
                gr.update(value="<div class='success'>✅ 文件已自动加载</div>", visible=True)
            ]

        demo.load(
            fn=load_initial_files,
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector, status_label],
            api_name=False
        )

        def select_all_invoice():
            valid_guids = []
            if current_guid in pbms_files:
                for f in pbms_files[current_guid]["files"]:
                    if f.get("attach_type") == "发票" and f["guid"] not in valid_guids:
                        valid_guids.append(f["guid"])
            return gr.update(value=valid_guids)

        def select_all_bill():
            valid_guids = []
            if current_guid in pbms_files:
                for f in pbms_files[current_guid]["files"]:
                    # 修改：只要attachtype包含"附件"两字，就归类到附件文件类别
                    if "附件" in f.get("attach_type", "") and f["guid"] not in valid_guids:
                        valid_guids.append(f["guid"])
            return gr.update(value=valid_guids)
        
        def select_all_overseas():  # 新增：境外票据全选函数
            valid_guids = []
            if current_guid in pbms_files:
                for f in pbms_files[current_guid]["files"]:
                    if f.get("attach_type") == "境外票据" and f["guid"] not in valid_guids:
                        valid_guids.append(f["guid"])
            return gr.update(value=valid_guids)

        def clear_selection():
            return gr.update(value=[])
        
        def select_all_global():
            """全选所有文件（按类别分别全选）"""
            if not current_guid or current_guid not in pbms_files:
                return [gr.update()]*4  # 如果无文件，返回空更新

            allowed_extensions = {'.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'}
            brno_guids = []
            invoice_guids = []
            bill_guids = []
            overseas_guids = []

            for f in pbms_files[current_guid]["files"]:
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
            return [
                gr.update(value=[]),  # BRNO
                gr.update(value=[]),  # 发票
                gr.update(value=[]),  # 附件
                gr.update(value=[])   # 境外票据
            ]

        # 全局按钮事件绑定
        global_select_all.click(
            fn=select_all_global,
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector]
        )

        global_clear_all.click(
            fn=clear_all_global,
            outputs=[brno_selector, invoice_selector, bill_selector, overseas_selector]
        )

        invoice_select_all.click(fn=select_all_invoice, outputs=invoice_selector)
        bill_select_all.click(fn=select_all_bill, outputs=bill_selector)
        overseas_select_all.click(fn=select_all_overseas, outputs=overseas_selector)  # 新增：境外票据全选绑定
        invoice_clear_all.click(fn=clear_selection, outputs=invoice_selector)
        bill_clear_all.click(fn=clear_selection, outputs=bill_selector)
        overseas_clear_all.click(fn=clear_selection, outputs=overseas_selector)  # 新增：境外票据清空绑定
        
        def update_merge_order(brno, invoice, bill, overseas, prev_order):
            # 合并所有选中的 guid，按用户点击顺序排列
            # 只保留当前被选中的 guid，顺序以 prev_order 为主，新增的加到末尾
            selected = brno + invoice + bill + overseas
            new_order = [g for g in prev_order if g in selected]
            for g in selected:
                if g not in new_order:
                    new_order.append(g)
            return new_order

        brno_selector.change(update_merge_order, [brno_selector, invoice_selector, bill_selector, overseas_selector, merge_order_state], merge_order_state)
        invoice_selector.change(update_merge_order, [brno_selector, invoice_selector, bill_selector, overseas_selector, merge_order_state], merge_order_state)
        bill_selector.change(update_merge_order, [brno_selector, invoice_selector, bill_selector, overseas_selector, merge_order_state], merge_order_state)
        overseas_selector.change(update_merge_order, [brno_selector, invoice_selector, bill_selector, overseas_selector, merge_order_state], merge_order_state)
        
        def merge_files(selected_guids: list, progress: gr.Progress = gr.Progress()):
            global current_guid
            temp_files = []
            failed_files = []  # 记录合并失败的文件名
            try:
                if not current_guid:
                    raise ValueError("请先通过API设置GUID")
                if current_guid not in pbms_files:
                    raise ValueError("文件未加载")
                
                # 合并所有选择的文件列表
                all_guids = selected_guids
                if not all_guids:
                    raise ValueError("请至少选择一个文件")
                
                merger = PdfMerger()
                brno_number = pbms_files[current_guid]["brno"]
                output_filename = f"{brno_number}.pdf" if brno_number else f"merged_{uuid.uuid4()}.pdf"
                output_dir = os.path.join(STATIC_DIR, "merged")
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, output_filename.replace("/", "_"))
                
                total_files = len(all_guids)
                
                progress(0, desc="正在准备文件...")
                for index, guid in enumerate(all_guids, 1):
                    file = next(f for f in pbms_files[current_guid]["files"] if f["guid"] == guid)
                    file_path = file["path"]
                    filename = file["filename"]
                    file_ext = os.path.splitext(file_path)[1].lower()
                    
                    try:
                        if file_ext in ('.png', '.jpg', '.jpeg'):
                            pdf_path = image_to_pdf(file_path)
                            temp_files.append(pdf_path)
                            # 图片转换后的PDF也要尝试读取，可能转换后的PDF也有问题，但概率很低
                            merger.append(pdf_path)
                        elif file_ext in ('.doc', '.docx'):
                            pdf_path = word_to_pdf(file_path)
                            temp_files.append(pdf_path)
                            merger.append(pdf_path)
                        else:
                            merger.append(file_path)
                    except PdfReadError as e:
                        print(f"无法读取文件 {filename}: {str(e)}")
                        failed_files.append(filename)
                    except Exception as e:
                        print(f"处理文件 {filename} 时发生错误: {str(e)}")
                        failed_files.append(filename)
                    
                    progress(index/total_files, desc=f"正在处理文件 {index}/{total_files}")
                
                merger.write(output_path)
                merger.close()
                
                preview_url = f"/static/merged/{os.path.basename(output_path)}"
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
                    gr.update(),
                    gr.update(),
                    gr.update()
                ]
            finally:
                # 清理临时文件
                for f in temp_files:
                    try:
                        os.remove(f)
                    except:
                        pass
                temp_pdf_dir = os.path.join(FILE_DIR, "temp_pdfs")
                if os.path.exists(temp_pdf_dir):
                    shutil.rmtree(temp_pdf_dir, ignore_errors=True)

        merge_btn.click(
            fn=merge_files,
            inputs=[merge_order_state],
            outputs=[file_link, status_label, invoice_selector, bill_selector, overseas_selector],
            api_name="merge_pdfs"
        )
        
    return demo

gradio_app = create_interface()
app = mount_gradio_app(app, gradio_app, path="/app")

@app.get("/")
async def root():
    return RedirectResponse("/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)