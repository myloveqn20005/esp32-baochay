import machine, time, dht, json, network, socket, os, esp32
import gc 
try:
    import urequests as requests
except ImportError:
    import requests

# ==========================================
# 1. QUẢN LÝ PHIÊN BẢN & CẤU HÌNH
# ==========================================
CURRENT_VERSION = "1.2"  # Đã nâng lên bản 1.2
CONFIG_FILE = "config.json"

# --- DÁN 3 ĐƯỜNG LINK CỦA BẠN VÀO ĐÂY ---
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbzorFwmW57CLc1QFI4lA6zA2G8R5MR8xILlMsNqJovmPsJxkMSive7HUjEngJOa-ueb/exec" 
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/TenCuaBan/RepoCuaBan/main/version.txt"
GITHUB_MAIN_URL = "https://raw.githubusercontent.com/TenCuaBan/RepoCuaBan/main/main.py"

default_config = {
    "ssid": "",
    "password": "",
    "blynk_token": "",
    "ntfy_topic": "baodong_quan_minhanh",
    "mq2_nguong": 2000,
    "mq5_nguong": 2000,
    "temp_nguong": 50,
    "last_ip": ""
}

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            for k in default_config:
                if k not in cfg: cfg[k] = default_config[k]
            return cfg
    except Exception:
        save_config(default_config)
        return default_config

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f)

app_config = load_config()

current_temp = 0
current_mq2 = 0
current_mq5 = 0

# ==========================================
# 2. KHỞI TẠO PHẦN CỨNG
# ==========================================
btn_reset = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
buzzer = machine.Pin(19, machine.Pin.OUT, value=1)
dht_sensor = dht.DHT22(machine.Pin(18))

mq2 = machine.ADC(machine.Pin(32))
mq2.atten(machine.ADC.ATTN_11DB)
mq5 = machine.ADC(machine.Pin(33))
mq5.atten(machine.ADC.ATTN_11DB)

try:
    import sh1106
    i2c = machine.I2C(scl=machine.Pin(22), sda=machine.Pin(21), freq=400000)
    display = sh1106.SH1106_I2C(128, 64, i2c, machine.Pin(16), 0x3c)
    has_oled = True
except:
    has_oled = False

# ==========================================
# 3. KẾT NỐI MẠNG (CÓ AP MODE DỰ PHÒNG)
# ==========================================
wlan_sta = network.WLAN(network.STA_IF)
wlan_ap = network.WLAN(network.AP_IF)

def setup_ap_mode():
    wlan_sta.active(False)
    wlan_ap.active(True)
    wlan_ap.config(essid="ESP32_BaoChay", authmode=0)
    ip = wlan_ap.ifconfig()[0]
    print(f"-> Phat Wi-Fi: 'ESP32_BaoChay' | IP: {ip}")
    return ip

def connect_wifi():
    if not app_config['ssid']:
        return setup_ap_mode()
    
    wlan_ap.active(False)
    wlan_sta.active(True)
    wlan_sta.connect(app_config['ssid'], app_config['password'])
    
    print(f"Dang ket noi: {app_config['ssid']} ...")
    start_time = time.time()
    while not wlan_sta.isconnected():
        if time.time() - start_time > 15:
            print("Ket noi that bai! Chuyen sang AP mode.")
            return setup_ap_mode()
        time.sleep(0.5)
        
    ip = wlan_sta.ifconfig()[0]
    print(f"-> Ket noi thanh cong! IP: {ip}")
    return ip

current_ip = connect_wifi()

# ==========================================
# 4. HÀM GỬI THÔNG BÁO VÀ DỮ LIỆU
# ==========================================
def send_ntfy_alert(msg, is_alarm=True):
    topic = app_config['ntfy_topic']
    if not topic or not wlan_sta.isconnected(): 
        return
    url = f"https://ntfy.sh/{topic}"
    headers = {"Title": "BAO DONG KHAN CAP", "Priority": "5", "Tags": "rotating_light,fire"} if is_alarm else {"Title": "THONG BAO HE THONG", "Priority": "3", "Tags": "information_source"}
    try:
        res = requests.post(url, data=msg.encode('utf-8'), headers=headers)
        res.close()
    except Exception:
        pass
    finally:
        gc.collect()

def send_to_google_sheet(t, m2, m5):
    if not wlan_sta.isconnected(): return
    try:
        url = f"{GOOGLE_SHEET_URL}?temp={t}&mq2={m2}&mq5={m5}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers)
        res.close()
    except Exception:
        pass
    finally:
        gc.collect()

# ==========================================
# 5. KHỞI TẠO WEB SERVER VÀ GIAO DIỆN
# ==========================================
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('', 80)) 
s.listen(5)
s.setblocking(False)

def parse_url(s):
    return s.replace('+', ' ').replace('%2F', '/').replace('%3D', '=')

def html_page():
    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ESP32 Dashboard</title>
    <style>
        body {font-family: Arial; padding: 10px; background: #222; color: #fff;}
        .card {background: #333; padding: 20px; border-radius: 8px; max-width: 400px; margin: auto; margin-bottom: 20px;}
        .stat {font-size: 20px; font-weight: bold; color: #ffeb3b;}
        input, select {width: 100%%; padding: 10px; margin: 8px 0; border-radius: 4px; box-sizing: border-box; border: none;}
        input[type="submit"] {background: #e63946; color: white; font-weight: bold; font-size: 16px; cursor: pointer; margin-top: 15px;}
        label {font-size: 12px; color: #aaa;}
        .flex-row {display: flex; gap: 8px; margin: 8px 0;}
        .flex-row input {margin: 0;}
        .btn-scan {background: #28a745; color: white; border: none; padding: 0 15px; border-radius: 4px; cursor: pointer; font-weight: bold;}
        .btn-fw {background: #2196f3; color: white; border: none; padding: 10px; cursor: pointer; width: 100%%; border-radius: 4px; font-weight: bold;}
        .btn-up {background: #4caf50; color: white; border: none; padding: 10px; cursor: pointer; width: 100%%; border-radius: 4px; font-weight: bold; display: none; margin-top: 10px;}
        .sys-info {font-size: 13px; color: #4caf50; margin: 0; padding-top: 10px; text-align: center;}
    </style></head><body>
    
    <div class="card">
        <h2 style="margin-top:0;">THỐNG KÊ HIỆN TẠI</h2>
        <p>Nhiệt độ: <span class="stat" id="val_temp">%s °C</span></p>
        <p>Khói MQ-2: <span class="stat" id="val_mq2">%s</span></p>
        <p>Gas MQ-5: <span class="stat" id="val_mq5">%s</span></p>
        
        <hr style="border:0; border-top:1px solid #555; margin:15px 0;">
        <p class="sys-info">
            💻 CPU: <span id="v_cpu">-</span> MHz &nbsp;|&nbsp; 
            🧠 RAM: <span id="v_ram">-</span> KB &nbsp;|&nbsp; 
            🔥 Lõi: <span id="v_core">-</span> °C
        </p>
    </div>

    <div class="card">
        <h2 style="margin-top:0;">CẬP NHẬT PHẦN MỀM</h2>
        <p>Bản hiện tại: <b>v%s</b></p>
        <button class="btn-fw" onclick="checkFW()">KIỂM TRA BẢN MỚI</button>
        <p id="fw_stt" style="font-size:13px; color:#aaa; margin-top:10px;"></p>
        <button id="btn_up" class="btn-up" onclick="doFW()">ĐỒNG Ý CẬP NHẬT CODE</button>
    </div>

    <div class="card">
        <h2 style="margin-top:0;">CẤU HÌNH HỆ THỐNG</h2>
        <form action="/save" method="GET">
            <label>Tên Wi-Fi:</label>
            <div class="flex-row">
                <input type="text" name="ssid" id="ssid_input" value="%s">
                <button type="button" class="btn-scan" onclick="scanWifi()">DÒ</button>
            </div>
            <div id="wifi_result"></div>
            
            <label>Mật khẩu Wi-Fi:</label>
            <input type="password" name="pw" value="%s">
            <label>Blynk Token (Nếu dùng):</label>
            <input type="text" name="blynk" value="%s">
            <label>Ntfy Topic:</label>
            <input type="text" name="ntfy" value="%s">
            
            <hr style="border:0; border-top:1px solid #555; margin:15px 0;">
            <label>Ngưỡng báo Khói (MQ-2):</label>
            <input type="number" name="mq2" value="%s">
            <label>Ngưỡng báo Gas (MQ-5):</label>
            <input type="number" name="mq5" value="%s">
            <label>Ngưỡng báo Nhiệt độ (°C):</label>
            <input type="number" name="temp" value="%s">
            <input type="submit" value="LƯU & KHỞI ĐỘNG LẠI">
        </form>
    </div>
    
    <script>
    const CURRENT_VER = "%s";
    
    function scanWifi() {
        let res = document.getElementById('wifi_result');
        res.innerHTML = '<span style="color:#ffeb3b; font-size:12px;">Đang quét mạng...</span>';
        fetch('/scan').then(r => r.text()).then(html => {
            res.innerHTML = '<select onchange="document.getElementById(\\'ssid_input\\').value = this.value"><option value="">-- Chọn Wi-Fi --</option>' + html + '</select>';
        }).catch(e => { res.innerHTML = '<span style="color:red; font-size:12px;">Lỗi dò mạng!</span>'; });
    }

    setInterval(function() {
        fetch('/stats').then(r => r.json()).then(data => {
            document.getElementById('val_temp').innerText = data.t + ' °C';
            document.getElementById('val_mq2').innerText = data.m2;
            document.getElementById('val_mq5').innerText = data.m5;
            
            // Cập nhật thông số hệ thống
            document.getElementById('v_cpu').innerText = data.cpu;
            document.getElementById('v_ram').innerText = data.ram_f + '/' + data.ram_t;
            document.getElementById('v_core').innerText = data.core;
        }).catch(e => {});
    }, 2000);

    function checkFW() {
        document.getElementById('fw_stt').innerText = "Đang kết nối GitHub...";
        fetch('/check_fw').then(r=>r.text()).then(v => {
            let ver = v.trim();
            if(ver !== CURRENT_VER && ver !== '') {
                document.getElementById('fw_stt').innerText = "Phát hiện bản mới: v" + ver + ". Bấm nút xanh để cài đặt!";
                document.getElementById('btn_up').style.display = 'block';
            } else {
                document.getElementById('fw_stt').innerText = "Bạn đang dùng bản mới nhất!";
                document.getElementById('btn_up').style.display = 'none';
            }
        }).catch(e => { document.getElementById('fw_stt').innerText = "Lỗi kết nối máy chủ"; });
    }

    function doFW() {
        document.getElementById('fw_stt').innerText = "Đang tải Code... VUI LÒNG KHÔNG RÚT ĐIỆN!!! Mạch sẽ tự khởi động lại.";
        document.getElementById('btn_up').style.display = 'none';
        fetch('/do_fw').then(r=>r.text()).then(r => {
            setTimeout(() => location.reload(), 8000);
        }).catch(e => {});
    }
    </script>
    </body></html>""" % (
        current_temp, current_mq2, current_mq5,
        CURRENT_VERSION,
        app_config['ssid'], app_config['password'], 
        app_config['blynk_token'], app_config['ntfy_topic'],
        app_config['mq2_nguong'], app_config['mq5_nguong'], app_config['temp_nguong'],
        CURRENT_VERSION
    )
    return html

# ==========================================
# 6. VÒNG LẶP CHÍNH
# ==========================================
last_read_time = 0
btn_press_start = 0
last_ntfy_time = 0 
last_sheet_time = 0 

print(f"HỆ THỐNG BẮT ĐẦU CHẠY PHIÊN BẢN {CURRENT_VERSION}!")

# Kêu còi báo hiệu khởi động xong
buzzer.value(0); time.sleep(0.1); buzzer.value(1); time.sleep(0.1)
buzzer.value(0); time.sleep(0.1); buzzer.value(1)

if wlan_sta.isconnected():
    msg_boot = f"✅ Hệ thống khởi động thành công!\nPhiên bản: v{CURRENT_VERSION}\nLink Cài đặt: http://{current_ip}"
    send_ntfy_alert(msg_boot, is_alarm=False)
    
    if current_ip != app_config['last_ip']:
        app_config['last_ip'] = current_ip
        save_config(app_config)

while True:
    current_time = time.ticks_ms()
    
    # ---- NÚT BOOT: NHẤN GIỮ 3 GIÂY ĐỂ RESET WI-FI ----
    if btn_reset.value() == 0:
        if btn_press_start == 0:
            btn_press_start = current_time
        elif time.ticks_diff(current_time, btn_press_start) > 3000:
            buzzer.value(0); time.sleep(0.5); buzzer.value(1)
            app_config['ssid'] = ""
            app_config['password'] = ""
            app_config['last_ip'] = ""
            save_config(app_config)
            machine.reset() 
    else:
        btn_press_start = 0 

    # ---- XỬ LÝ CẢM BIẾN (2 GIÂY/LẦN) ----
    if time.ticks_diff(current_time, last_read_time) >= 2000:
        last_read_time = current_time
        
        current_mq2 = mq2.read()
        current_mq5 = mq5.read()
        try:
            dht_sensor.measure()
            current_temp = dht_sensor.temperature()
        except:
            current_temp = 0
            
        if has_oled:
            display.fill(0)
            display.text("GIAM SAT AN TOAN", 0, 0)
            display.text(f"Temp: {current_temp} C", 0, 20)
            display.text(f"Khoi: {current_mq2}  Gas: {current_mq5}", 0, 35)
            display.text(f"Ver: {CURRENT_VERSION} | IP:{current_ip[-3:]}", 0, 50)
            display.show()
            
        if (current_mq2 > app_config['mq2_nguong'] or 
            current_mq5 > app_config['mq5_nguong'] or 
            current_temp > app_config['temp_nguong']):
            
            buzzer.value(0)
            if time.ticks_diff(current_time, last_ntfy_time) > 60000:
                msg = f"Phat hien vuot nguong an toan!\nNhiet: {current_temp}°C\nKhoi: {current_mq2}\nGas: {current_mq5}"
                send_ntfy_alert(msg, True)
                last_ntfy_time = current_time
        else:
            buzzer.value(1)

    # ---- ĐẨY DỮ LIỆU LÊN GOOGLE SHEETS (MỖI 10 GIÂY) ----
    if time.ticks_diff(current_time, last_sheet_time) >= 10000:
        last_sheet_time = current_time
        send_to_google_sheet(current_temp, current_mq2, current_mq5)

    # ---- XỬ LÝ WEB SERVER ----
    try:
        conn, addr = s.accept()
        try:
            request = conn.recv(1024).decode('utf-8')
            
            # --- XỬ LÝ LƯU CẤU HÌNH ---
            if '/save?' in request:
                try:
                    params_str = request.split(' ')[1].split('?')[1]
                    params = params_str.split('&')
                    for param in params:
                        if '=' not in param: continue
                        key, val = param.split('=', 1)
                        val = parse_url(val)
                        if key in app_config:
                            app_config[key] = int(val) if 'nguong' in key else val
                    
                    save_config(app_config)
                    
                    html_success = """HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n
                    <!DOCTYPE html><html><head><meta charset="utf-8">
                    <meta name='viewport' content='width=device-width, initial-scale=1'></head>
                    <body style='background:#222; color:#fff; text-align:center; padding-top:50px; font-family:Arial;'>
                    <h1 style='color:#4caf50;'>ĐÃ LƯU THÀNH CÔNG!</h1><p>Hệ thống đang khởi động lại...</p></body></html>"""
                    
                    conn.send(html_success.encode('utf-8'))
                    time.sleep(2)
                    machine.reset() 
                except Exception:
                    pass
            
            # --- XỬ LÝ DÒ MẠNG WI-FI ---
            elif '/scan' in request:
                gc.collect() 
                try:
                    wlan_sta.active(True)
                    networks = wlan_sta.scan()
                    ssids = []
                    for net in networks:
                        ssid = net[0].decode('utf-8')
                        if ssid and ssid not in ssids:
                            ssids.append(ssid)
                    options = "".join([f"<option value='{s}'>{s}</option>" for s in ssids])
                    
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n'.encode('utf-8'))
                    conn.send(options.encode('utf-8'))
                except Exception:
                    pass
            
            # --- OTA: KIỂM TRA PHIÊN BẢN ---
            elif '/check_fw' in request:
                gc.collect()
                try:
                    res = requests.get(GITHUB_VERSION_URL)
                    git_ver = res.text.strip()
                    res.close()
                    conn.send('HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n'.encode('utf-8'))
                    conn.send(git_ver.encode('utf-8'))
                except Exception:
                    conn.send('HTTP/1.1 500 ERROR\r\n\r\n'.encode('utf-8'))
                    
            # --- OTA: TẢI CODE MỚI VÀ CẬP NHẬT ---
            elif '/do_fw' in request:
                gc.collect()
                try:
                    res = requests.get(GITHUB_MAIN_URL)
                    new_code = res.text
                    res.close()
                    if len(new_code) > 1000: 
                        with open('main.py', 'w') as f: f.write(new_code)
                        conn.send('HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nOK'.encode('utf-8'))
                        time.sleep(2); machine.reset() 
                    else:
                        conn.send('HTTP/1.1 500 ERROR\r\n\r\n'.encode('utf-8'))
                except Exception:
                    conn.send('HTTP/1.1 500 ERROR\r\n\r\n'.encode('utf-8'))

            # --- AJAX LẤY THÔNG SỐ (Bao gồm System Info) ---
            elif '/stats' in request:
                try:
                    # Lấy cấu hình phần cứng
                    cpu_mhz = machine.freq() // 1000000
                    ram_free = gc.mem_free() // 1024
                    ram_total = (gc.mem_free() + gc.mem_alloc()) // 1024
                    
                    # ESP32 đo bằng độ F, dùng công thức đổi sang độ C
                    try:
                        core_temp = round((esp32.raw_temperature() - 32) * 5/9, 1)
                    except:
                        core_temp = 0
                        
                    stats_json = f'{{"t": {current_temp}, "m2": {current_mq2}, "m5": {current_mq5}, "cpu": {cpu_mhz}, "ram_f": {ram_free}, "ram_t": {ram_total}, "core": {core_temp}}}'
                    
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n'.encode('utf-8'))
                    conn.send(stats_json.encode('utf-8'))
                except Exception:
                    pass
                    
            # --- TRẢ VỀ GIAO DIỆN CHÍNH ---
            else:
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n'.encode('utf-8'))
                conn.send(html_page().encode('utf-8'))
                
        except Exception:
            pass
        finally:
            conn.close()
            gc.collect()
            
    except OSError:
        pass
