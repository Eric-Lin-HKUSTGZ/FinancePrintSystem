import os
import requests
from requests.auth import HTTPBasicAuth

def download_file(response, save_dir, guid, file_type, filename_from_api=None):
    """处理文件下载和保存，优先使用API提供的文件名"""
    try:
        # 优先使用API提供的解码后的文件名
        if filename_from_api:
            filename = filename_from_api
        else:
            # 次优先从响应头获取文件名
            content_disposition = response.headers.get('Content-Disposition', '')
            if 'filename=' in content_disposition:
                filename = content_disposition.split('filename=')[1].strip('"')
                # 处理可能的URL编码或UTF-8编码
                filename = filename.encode('latin-1').decode('utf-8', errors='ignore')
            else:
                # 根据文件类型生成默认文件名
                ext = "pdf"
                if 'file' in file_type.lower():
                    
                    ext = "pdf"
                filename = f"{file_type}_{guid}.{ext}"

        # 确保文件名有效，替换可能的非法字符
        filename = filename.replace('/', '_').replace('\\', '_')
        
        # 创建保存目录
        os.makedirs(save_dir, exist_ok=True)
        
        # 保存文件
        save_path = os.path.join(save_dir, filename)
        with open(save_path, 'wb') as f:
            # for chunk in response.iter_content(chunk_size=8192):
            #     if chunk:  # 过滤掉保持连接的空白块
            #         f.write(chunk)
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"文件已保存至: {save_path}")
        return True
    except Exception as e:
        print(f"下载失败 {file_type.upper()} {guid}: {str(e)}")
        return False

def test_external_guid_endpoint():
    """测试主接口及文件下载功能"""
    # 初始化参数
    # initial_guid = "d37f80dbc5594f1bb9150f80166d7e79"
    # initial_guid = "f9f7578fdb6b4ef7b5d823dda1db06d5"  # 替换为实际的初始GUID
    initial_guid = "706b4cbdaaef4fbba04a26c506681b19"  # 测试用GUID
    base_url = "https://pbms.hkust-gz.edu.cn"
    auth = HTTPBasicAuth("brgpt", "jiyMBV432-HAS98")
    
    # 第一步：获取主GUID数据
    response = requests.post(
        f"{base_url}/api/br/BRFileLists",
        params={"guid": initial_guid},
        auth=auth,
        headers={"Content-Type": "application/json; charset=utf-8"}
    )

    # 让中文正常显示
    print(f"GUID包含的内容:{response.content.decode('utf-8')}")
    
    assert response.status_code == 200, f"主请求失败: {response.text}"
    data = response.json()
    
    # 第二步：分类提取GUID及文件名
    brno_guid_list = []
    file_info_list = []  # 存储元组(Guid, DecodedFileName)
    attachtype_list = [] # 存储附件文件类型信息
    
    for item in data.get("Data", []):
        # 提取BRNO级Guid
        if "Guid" in item:
            brno_guid_list.append(item["Guid"])
        
        # 提取Files中的信息
        for file in item.get("Files", []):
            if "Guid" in file:
                raw_filename = file.get("FileName", "")
                raw_attachtype = file.get("AttachType", "")
                # 处理中文编码问题：Latin-1转UTF-8
                try:
                    decoded_name = raw_filename.encode('latin-1').decode('utf-8')
                    decoded_attachtype = raw_attachtype.encode('latin-1').decode('utf-8')
                except:
                    decoded_name = raw_filename  # 解码失败保留原始名称
                    decoded_attachtype = raw_attachtype
                file_info_list.append( (file["Guid"], decoded_name) )
                attachtype_list.append((file["Guid"], decoded_attachtype))
    
    print(f"BRNO GUID列表: {brno_guid_list}")
    print(f"FILE信息列表: {[info[1] for info in file_info_list]}")
    print(f"ATTACHTYPE信息列表: {[info[1] for info in attachtype_list]}")
    
    # 第三步：处理BRNO类型下载（无文件名，依赖响应头）
    for idx, guid in enumerate(brno_guid_list, 1):
        print(f"正在处理第 {idx} 个BRNO GUID: {guid}")
        download_url = f"{base_url}/br/sysdownload?g={guid}"
        save_dir = "./brno"
        
        try:
            response = requests.post(
                download_url,
                auth=auth,
                headers={"Content-Type": "application/pdf; charset=utf-8"},
                stream=True
            )
            
            if response.status_code == 200:
                success = download_file(response, save_dir, guid, "brno")
                if not success:
                    print(f"文件保存失败: {guid}")
            else:
                print(f"BRNO请求失败，状态码: {response.status_code}")
                
        except Exception as e:
            print(f"BRNO请求异常: {str(e)}")
    
    # 第四步：处理FILE类型下载（使用API提供的解码后的文件名）
    for idx, (guid, filename) in enumerate(file_info_list, 1):
        print(f"正在处理第 {idx} 个FILE: {filename}")
        download_url = f"{base_url}/file/download?g={guid}"
        save_dir = "./file"
        
        try:
            response = requests.post(
                download_url,
                auth=auth,
                headers={"Content-Type": "application/pdf; charset=utf-8"},
                stream=True
            )
            
            if response.status_code == 200:
                # 传入解码后的文件名
                success = download_file(response, save_dir, guid, "file", filename_from_api=filename)
                if not success:
                    print(f"文件保存失败: {guid}")
            else:
                print(f"FILE请求失败，状态码: {response.status_code}")
                
        except Exception as e:
            print(f"FILE请求异常: {str(e)}")

if __name__ == "__main__":
    # 创建保存目录
    os.makedirs("./brno", exist_ok=True)
    os.makedirs("./file", exist_ok=True)
    
    test_external_guid_endpoint()