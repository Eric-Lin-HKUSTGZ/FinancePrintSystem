import requests

BASE_URL = "http://localhost:8080"
# BASE_URL = "http://10.120.20.213:15202"

def test_guid_setting():
    """测试GUID设置接口"""
    # test_guid = "4dec84e12cc54e73924dc9948e04269b" # docx含签名转pdf时存在丢失
    # test_guid = "e5210ca905674ee793cd3c3d1dfad66e"  # 电商BR
    # test_guid = "ea4e689c5a524700b5621eeca6d029c0" # 无邀请函
    # test_guid = "2eed381054bc4fed8dc21d40cda47c52" # brno出错
    # test_guid = "c592c289c162400aaeb68ccdd091d088" # 合并内容缺失
    # test_guid = "7294512a8c844074a1fe5acaef7c8919"  # 合并失败
    # test_guid = "6eb227bc252045c59499dce3b232e6c0"  # 测试用GUID
    # test_guid = "aba8175839fa408ebb2e11162da838c7"  # 下载异常
    # test_guid = "a4992db016174635922f8be64db385e3"
    # test_guid = "8de1cab34a094ac1b3604559288582fe"  # 压缩文件
    # test_guid = "f9f7578fdb6b4ef7b5d823dda1db06d5"
    test_guid = "706b4cbdaaef4fbba04a26c506681b19"
    
    # 发送POST请求
    response = requests.post(
        f"{BASE_URL}/api/set_guid",
        json={"guid": test_guid}
    )
    
    # 验证基础响应
    assert response.status_code == 200, "状态码异常"
    assert response.headers["Content-Type"] == "application/json", "响应类型错误"
    
    # 解析JSON数据
    response_data = response.json()
    
    # 验证响应结构
    assert "status" in response_data, "缺少状态字段"
    assert "message" in response_data, "缺少消息字段"
    assert "guid" in response_data, "缺少GUID字段"
    
    # 验证数据一致性
    assert response_data["status"] == "success", "状态非success"
    assert response_data["guid"] == test_guid, "返回GUID不一致"
    assert test_guid in response_data["message"], "消息内容不匹配"
    
    print("✅ GUID设置测试通过")

if __name__ == "__main__":
    print("🚀 启动简易测试")
    test_guid_setting()
    print("🎉 测试完成")

