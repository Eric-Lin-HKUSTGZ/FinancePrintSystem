import requests
import threading
import time

API_URL = "http://localhost:8080/api/set_guid"
SESSION_INFO_URL = "http://localhost:8080/api/session/{}"

# 你给定的guid列表
guids = [
    "4dec84e12cc54e73924dc9948e04269b",
    "e5210ca905674ee793cd3c3d1dfad66e",
    "ea4e689c5a524700b5621eeca6d029c0",
    "2eed381054bc4fed8dc21d40cda47c52",
    "c592c289c162400aaeb68ccdd091d088",
    "7294512a8c844074a1fe5acaef7c8919",
    "aba8175839fa408ebb2e11162da838c7"
]

def simulate_user(user_idx, guid):
    print(f"User {user_idx} using guid {guid}")
    resp = requests.post(API_URL, json={"guid": guid})
    print(f"User {user_idx} set_guid resp: {resp.json()}")
    session_id = resp.json().get("session_id")
    if not session_id:
        print(f"User {user_idx} failed to get session_id!")
        return
    # 轮询文件状态
    for i in range(15):
        time.sleep(1)
        info = requests.get(SESSION_INFO_URL.format(session_id)).json()
        print(f"User {user_idx} session info: {info}")
        if info.get("file_count", 0) > 0 and not info.get("processing", True):
            print(f"User {user_idx} files loaded.")
            break

threads = []
for i, guid in enumerate(guids):
    t = threading.Thread(target=simulate_user, args=(i, guid))
    t.start()
    threads.append(t)

for t in threads:
    t.join()