from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr
from gradio.routes import mount_gradio_app
import os
import uuid
from typing import Dict, List, Tuple
from PyPDF2 import PdfMerger
import tempfile
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image
import img2pdf
import shutil

# 配置参数
AUTH_USER = "brgpt"
AUTH_PASS = "jiyMBV432-HAS98"
BASE_URL = "https://pbmstest.hkust-gz.edu.cn"
STATIC_DIR = "static"
BRNO_DIR = "./brno"
FILE_DIR = "./file"
TEMP_DIR = tempfile.gettempdir()

# 创建目录
os.makedirs(BRNO_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "merged"), exist_ok=True)

app = FastAPI()

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

pbms_files: Dict[str, Dict] = {}

def process_guids(initial_guid: str) -> Tuple[str, List[Tuple[str, str, str]], List[Tuple[str, str, str]]]:
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
                try:
                    decoded_name = raw_name.encode('latin-1').decode('utf-8')
                except:
                    decoded_name = raw_name
                file_guids.append(("file", file["Guid"], decoded_name))
    
    return brno_number, brno_guids, file_guids

def download_file(file_type: str, guid: str, decoded_name: str = None) -> Tuple[Dict, str]:
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
            return None, f"HTTP {response.status_code}"
        
        if decoded_name:
            filename = decoded_name
        else:
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
                
        return {
            "guid": guid,
            "filename": final_name,
            "path": save_path,
            "type": file_type
        }, None
    except Exception as e:
        return None, str(e)

def image_to_pdf(image_path: str) -> str:
    pdf_path = os.path.splitext(image_path)[0] + ".pdf"
    
    if image_path.lower().endswith(('.png', '.jpg', '.jpeg')):
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_path))
    else:
        img = Image.open(image_path)
        img.save(pdf_path, "PDF", resolution=100.0)
    
    return pdf_path

def clean_directories():
    dirs_to_clean = [BRNO_DIR, FILE_DIR, os.path.join(STATIC_DIR, "merged")]
    for dir_path in dirs_to_clean:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)
        os.makedirs(dir_path, exist_ok=True)

def create_interface():
    with gr.Blocks(title="PBMS文件合并工具") as demo:
        gr.Markdown("""
        ## 📖 操作指南
        1. **输入GUID**  
           在下方输入框输入PBMS系统的GUID（如：`c965be9f9c1d449d9e50fed330150d7a`）
        2. **加载文件**  
           点击 <span style='color:green;font-weight:bold'>加载文件</span> 按钮获取文件列表
        3. **选择文件**  
           可使用 <span style='color:green;font-weight:bold'>全选PDF</span> / <span style='color:blue;font-weight:bold'>全选图片</span> 快速选择  
           可使用 <span style='color:red;font-weight:bold'>取消全选</span> 进行选择状态重置
        4. **合并操作**  
           点击 <span style='color:orange;font-weight:bold'>开始合并</span> 生成PDF文件
           直接点击生成的蓝色文件名即可预览
        """)

        with gr.Row():
            pbms_guid = gr.Textbox(
                label="PBMS GUID",
                placeholder="在此输入GUID（例如：c965be9f9c1d449d9e50fed330150d7a）",
                interactive=True,
                max_lines=1
            )
            load_files_btn = gr.Button("📥 加载文件", variant="primary")
        
        with gr.Row():
            brno_selector = gr.CheckboxGroup(label="BRNO文件（可选）")
        
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    pdf_select_all = gr.Button("全选PDF", size="sm")
                    pdf_clear_all = gr.Button("取消PDF全选", size="sm")
                pdf_selector = gr.CheckboxGroup(label="PDF文件", elem_classes="pdf-selector")
            
            with gr.Column():
                with gr.Row():
                    image_select_all = gr.Button("全选图片", size="sm")
                    image_clear_all = gr.Button("取消图片全选", size="sm")
                image_selector = gr.CheckboxGroup(label="图片文件", elem_classes="image-selector")
        
        with gr.Row():
            merge_btn = gr.Button("开始合并", variant="primary")
            file_link = gr.HTML(visible=False)  # 替换为HTML组件
            status_label = gr.Textbox(label="状态提示", visible=False)

        @load_files_btn.click(
            inputs=pbms_guid,
            outputs=[brno_selector, pdf_selector, image_selector, status_label],
            api_name=False
        )
        def load_files(guid: str):
            try:
                clean_directories()
                
                if not guid.strip():
                    raise ValueError("GUID不能为空")
            
                if guid in pbms_files:
                    del pbms_files[guid]
                
                brno_number, brno_items, file_items = process_guids(guid)
                pbms_files[guid] = {"brno": brno_number, "files": []}

                for file_type, g, _ in brno_items:
                    file_info, error = download_file(file_type, g)
                    if not error:
                        pbms_files[guid]["files"].append(file_info)
                
                for file_type, g, name in file_items:
                    file_info, error = download_file(file_type, g, decoded_name=name)
                    if not error:
                        pbms_files[guid]["files"].append(file_info)
                
                brno_files = []
                pdf_files = []
                image_files = []
                
                for f in pbms_files[guid]["files"]:
                    if f["type"] == "brno":
                        brno_files.append(f)
                    elif f["type"] == "file":
                        ext = f["filename"].lower().split('.')[-1]
                        if ext == "pdf":
                            pdf_files.append(f)
                        elif ext in ["jpg", "jpeg", "png", "gif"]:
                            image_files.append(f)
                
                return [
                    gr.update(choices=[(f["filename"], f["guid"]) for f in brno_files]),
                    gr.update(choices=[(f["filename"], f["guid"]) for f in pdf_files]),
                    gr.update(choices=[(f["filename"], f["guid"]) for f in image_files]),
                    gr.update(value="文件加载成功", visible=True)
                ]
                
            except Exception as e:
                error_msg = f"文件加载失败: {str(e)}"
                print(error_msg)
                return [
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(choices=[]),
                    gr.update(value=error_msg, visible=True)
                ]

        def get_valid_guids(current_guid: str, component_type: str):
            if current_guid not in pbms_files:
                return []
            
            valid_guids = []
            for f in pbms_files[current_guid]["files"]:
                if component_type == "pdf":
                    if f["type"] == "file" and f["filename"].lower().endswith(".pdf"):
                        valid_guids.append(f["guid"])
                elif component_type == "image":
                    ext = f["filename"].lower().split('.')[-1]
                    if f["type"] == "file" and ext in ["jpg", "jpeg", "png", "gif"]:
                        valid_guids.append(f["guid"])
            return valid_guids

        def select_all_pdf(current_guid: str):
            return gr.update(value=get_valid_guids(current_guid, "pdf"))

        def select_all_image(current_guid: str):
            return gr.update(value=get_valid_guids(current_guid, "image"))

        def clear_selection():
            return gr.update(value=[])

        pdf_select_all.click(
            fn=select_all_pdf,
            inputs=[pbms_guid],
            outputs=pdf_selector
        )
        image_select_all.click(
            fn=select_all_image,
            inputs=[pbms_guid],
            outputs=image_selector
        )
        pdf_clear_all.click(fn=clear_selection, outputs=pdf_selector)
        image_clear_all.click(fn=clear_selection, outputs=image_selector)

        def merge_files(brno_guids: list, pdf_guids: list, image_guids: list, current_guid: str, progress: gr.Progress = gr.Progress()):
            try:
                if current_guid not in pbms_files:
                    raise ValueError("请先加载文件")
                
                all_guids = brno_guids + pdf_guids + image_guids
                if not all_guids:
                    raise ValueError("请至少选择一个文件")
                
                merger = PdfMerger()
                brno_number = pbms_files[current_guid]["brno"]
                output_filename = f"{brno_number}.pdf" if brno_number else f"merged_{uuid.uuid4()}.pdf"
                output_dir = os.path.join(STATIC_DIR, "merged")
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, output_filename.replace("/", "_"))
                
                temp_files = []
                total_files = len(all_guids)
                
                progress(0, desc="正在准备文件...")
                for index, guid in enumerate(all_guids, 1):
                    file = next(f for f in pbms_files[current_guid]["files"] if f["guid"] == guid)
                    
                    if file["path"].lower().endswith(('.png', '.jpg', '.jpeg')):
                        pdf_path = image_to_pdf(file["path"])
                        temp_files.append(pdf_path)
                        merger.append(pdf_path)
                    else:
                        merger.append(file["path"])
                    
                    progress(index/total_files, desc=f"正在处理文件 {index}/{total_files}")
                
                merger.write(output_path)
                merger.close()
                
                # 生成可直接点击的链接
                preview_url = f"/static/merged/{os.path.basename(output_path)}"
                html_content = f"""
                    <div style="margin-top:10px">
                        <a href="{preview_url}" 
                           target="_blank" 
                           style="color: #2563eb; 
                                  text-decoration: underline;
                                  font-weight: 500;
                                  cursor: pointer;">
                            📄 {output_filename}
                        </a>
                    </div>
                """
                
                for f in temp_files:
                    try:
                        os.remove(f)
                    except:
                        pass
                
                return [
                    gr.update(value=html_content, visible=True),
                    gr.update(value=f"合并完成: {output_filename}", visible=True),
                    gr.update(value=[]),
                    gr.update(value=[])
                ]
                
            except Exception as e:
                error_msg = f"合并失败: {str(e)}"
                print(error_msg)
                return [
                    gr.update(visible=False),
                    gr.update(value=error_msg, visible=True),
                    gr.update(),
                    gr.update()
                ]

        merge_btn.click(
            fn=merge_files,
            inputs=[brno_selector, pdf_selector, image_selector, pbms_guid],
            outputs=[file_link, status_label, pdf_selector, image_selector],
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