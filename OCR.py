import os
import sys
import tempfile
import datetime
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QFileDialog, QMessageBox,
    QSystemTrayIcon, QMenu, QRubberBand
)
from PySide6.QtGui import (
    QPixmap, QPainter, QImage, QIcon, QPainterPath, QColor, QAction,
    QFont, QLinearGradient, QBrush
)
from PySide6.QtCore import (
    Qt, QPoint, QRect, QRectF, QTimer, Signal, QSize, QThread, QObject
)
from PySide6 import QtWidgets, QtCore

import pyperclip
import mss
from PIL import Image, ImageDraw, ImageFont


# ========== 全局异常捕获 ==========
def handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log_path = os.path.expanduser("~/ocr_crash.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"CRASH: {exc_value}\n")
        import traceback
        traceback.print_exception(exc_type, exc_value, exc_tb, file=f)

sys.excepthook = handle_exception


# ========== 路径工具 ==========
def get_engine_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, "rapidocr", "RapidOcrOnnx.exe")

def get_models_dir():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, "rapidocr", "models")


# ========== Windows 前台窗口==========
if sys.platform == "win32":
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    def bring_window_to_front(hwnd):
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        user32.AttachThreadInput(foreground_thread, current_thread, True)
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(foreground_thread, current_thread, False)


# ========== 生成 OCR 托盘图标 ==========
def create_ocr_icon():
    img = Image.new('RGBA', (32, 32), (34, 139, 230, 255))  # 主色调背景
    draw = ImageDraw.Draw(img)
    # 加载系统默认字体并指定大小（12号字适配32x32图标）
    font = ImageFont.load_default(size=12)
    draw.text((2, 8), "OCR", fill=(255, 255, 255, 255), font=font)
    return img


# ========== OCR 结果过滤==========
def filter_ocr_bytes(raw_bytes):
    error_marker = "【错误输出】".encode("utf-8")
    if error_marker in raw_bytes:
        raw_bytes = raw_bytes.split(error_marker)[0]
    
    detect_marker = b"FullDetectTime"
    if detect_marker in raw_bytes:
        detect_start = raw_bytes.find(detect_marker)
        line_end = raw_bytes.find(b"\n", detect_start)
        if line_end != -1:
            result_bytes = raw_bytes[line_end + 1:]
        else:
            result_bytes = raw_bytes[detect_start + len(detect_marker):]
    else:
        result_bytes = raw_bytes
    
    try:
        result_str = result_bytes.decode("utf-8")
    except UnicodeDecodeError:
        result_str = result_bytes.decode("gbk", errors="replace")
    
    lines = [line.strip() for line in result_str.split('\n') if line.strip()]
    return '\n'.join(lines) if lines else "未识别到有效文本"


# ========== OCR 工作线程==========
class OCRWorker(QObject):
    result_ready = Signal(str)
    error_occurred = Signal(str)
    
    def __init__(self, image_path):
        super().__init__()
        self.image_path = image_path

    def run(self):
        try:
            engine_path = get_engine_path()
            models_dir = get_models_dir()
            
            if not os.path.exists(engine_path):
                raise FileNotFoundError(f"OCR 引擎未找到: {engine_path}")
            if not os.path.isdir(models_dir):
                raise FileNotFoundError(f"模型目录缺失: {models_dir}")

            cmd = [
                engine_path,
                "--models", models_dir,
                "--det", "ch_PP-OCRv4_det_infer.onnx",
                "--cls", "ch_ppocr_mobile_v2.0_cls_infer.onnx",
                "--rec", "ch_PP-OCRv4_rec_infer.onnx",
                "--keys", "ppocr_keys_v1.txt",
                "--image", self.image_path,
                "--numThread", "4",
                "--GPU", "-1"
            ]

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

            stdout_marker = "【标准输出】\n".encode("utf-8")
            stderr_marker = "\n【错误输出】\n".encode("utf-8")
            raw_bytes = stdout_marker + result.stdout + stderr_marker + result.stderr
            pure_text = filter_ocr_bytes(raw_bytes)
            if "未识别到有效文本" == pure_text or "识别失败" in pure_text:
                self.error_occurred.emit(pure_text)
            else:
                self.result_ready.emit(pure_text)
        except Exception as e:
            self.error_occurred.emit(f"OCR 执行异常: {str(e)}")

# ========== 截图部件 (美化选框和遮罩) ==========
class ScreenshotWidget(QtWidgets.QWidget):
    screenshot_taken = Signal(object)
    
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowState(Qt.WindowFullScreen)
        self.setCursor(Qt.CrossCursor)
        
        with mss.mss() as sct:
            self.monitor = sct.monitors[1]
            screenshot = sct.grab(self.monitor)
            self.pil_image = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            self.physical_width = screenshot.width
            self.physical_height = screenshot.height
        
        screen = QApplication.primaryScreen()
        logical_rect = screen.geometry()
        self.logical_width = logical_rect.width()
        self.logical_height = logical_rect.height()
        self.scale_x = self.physical_width / self.logical_width
        self.scale_y = self.physical_height / self.logical_height
        
        img_data = self.pil_image.tobytes()
        qimg = QImage(img_data, self.pil_image.width, self.pil_image.height, QImage.Format_RGB888)
        self.bg_pixmap = QPixmap.fromImage(qimg)
        
        self.mask_opacity = 0.5  # 提高遮罩透明度，更清晰
        self.origin = QPoint()
        self.current_rect = QRect()
        self.rubberband = QRubberBand(QRubberBand.Rectangle, self)
        # 美化选框样式
        self.rubberband.setStyleSheet("""
            QRubberBand {
                border: 2px solid #2196F3;
                background-color: rgba(33, 150, 243, 0.1);
            }
        """)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.bg_pixmap)
        painter.save()
        painter.setOpacity(self.mask_opacity)
        full_rect_f = QRectF(self.rect())
        path = QPainterPath()
        path.addRect(full_rect_f)
        if not self.current_rect.isNull():
            current_rect_f = QRectF(self.current_rect)
            path.addRect(current_rect_f)
            path.setFillRule(Qt.OddEvenFill)  # 更自然的遮罩效果
        painter.fillPath(path, QColor(0, 0, 0))
        painter.restore()
    
    def mousePressEvent(self, event):
        self.origin = event.position().toPoint()
        self.current_rect = QRect(self.origin, event.position().toPoint()).normalized()
        self.rubberband.setGeometry(self.current_rect)
        self.rubberband.show()
    
    def mouseMoveEvent(self, event):
        new_rect = QRect(self.origin, event.position().toPoint()).normalized()
        if new_rect != self.current_rect:
            self.current_rect = new_rect
            self.rubberband.setGeometry(self.current_rect)
            self.update()
    
    def mouseReleaseEvent(self, event):
        self.rubberband。hide()
        rect = self.current_rect
        if rect.width() <= 10 or rect.height() <= 10:
            self.close()
            return
        
        x1 = int(rect.x() * self.scale_x)
        y1 = int(rect.y() * self.scale_y)
        x2 = int((rect.x() + rect.width()) * self.scale_x)
        y2 = int((rect.y() + rect.height()) * self.scale_y)
        x1 = max(0, min(x1, self.physical_width - 1))
        y1 = max(0, min(y1, self.physical_height - 1))
        x2 = max(1, min(x2, self.physical_width))
        y2 = max(1, min(y2, self.physical_height))
        
        if x2 <= x1 or y2 <= y1:
            self.close()
            return
        
        cropped = self.pil_image.crop((x1, y1, x2, y2))
        self.screenshot_taken.emit(cropped)
        self.close()


# ========== 主窗口 =======
class OCRMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # 窗口基础设置
        self.setWindowTitle("截屏OCR工具")
        self.resize(800, 500)  # 默认窗口尺寸
        self.setMinimumSize(600, 400)
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint)
        
        # 窗口圆角和背景 (重写paintEvent实现)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("""
            QMainWindow {
                background: transparent;
            }
        """)
        
        # 居中显示
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())
        
        # 主容器（用于实现圆角）
        self.main_container = QWidget()
        self.setCentralWidget(self.main_container)
        self.main_container.setStyleSheet("""
            QWidget {
                background-color: #f8f9fa;
                border-radius: 12px;
            }
        """)
        
        # 主布局
        main_layout = QVBoxLayout(self.main_container)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        
        # 1. 功能按钮区域 (顶部)
        btn_h_layout = QHBoxLayout()
        btn_h_layout.setSpacing(15)
        
        # 截图按钮
        self.screenshot_btn = QPushButton("截图并OCR")
        self.screenshot_btn.setFixedHeight(55)
        self.screenshot_btn.setCursor(Qt.PointingHandCursor)
        self.screenshot_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2196F3, stop:1 #1976D2);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #42A5F5, stop:1 #2196F3);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1976D2, stop:1 #1565C0);
            }
        """)
        self.screenshot_btn.clicked.connect(self.start_screenshot)
        btn_h_layout.addWidget(self.screenshot_btn)
        
        # 选择图片按钮
        self.select_image_btn = QPushButton("选取图片并OCR")
        self.select_image_btn.setFixedHeight(55)
        self.select_image_btn.setCursor(Qt.PointingHandCursor)
        self.select_image_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #4CAF50, stop:1 #388E3C);
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #66BB6A, stop:1 #4CAF50);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #388E3C, stop:1 #2E7D32);
            }
        """)
        self.select_image_btn.clicked.connect(self.select_image_for_ocr)
        btn_h_layout.addWidget(self.select_image_btn)
        
        main_layout.addLayout(btn_h_layout)
        
        # 2. 内容展示区域 (中间)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(15)
        
        # 图片显示区域
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("""
            QLabel {
                background-color: #ffffff;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        self.image_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        content_layout.addWidget(self.image_label, 1)
        
        # 右侧文本区域
        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)
        
        # 文本编辑框
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("✨ 识别结果将显示在这里\n\n支持：截图识别 / 本地图片识别\n识别完成后可一键复制或保存")
        self.text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.5;
                color: #333333;
            }
            QTextEdit:focus {
                border-color: #2196F3;
                outline: none;
            }
        """)
        self.text_edit.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        right_layout.addWidget(self.text_edit)
        
        # 操作按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        # 复制按钮
        self.copy_btn = QPushButton("复制到剪贴板")
        self.copy_btn.setFixedHeight(40)
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #f5f5f5;
                color: #333333;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #eeeeee;
                border-color: #d0d0d0;
            }
            QPushButton:pressed {
                background-color: #e0e0e0;
            }
        """)
        self.copy_btn.clicked.connect(self.copy_text)
        btn_layout.addWidget(self.copy_btn)
        
        # 保存按钮
        self.save_btn = QPushButton("另存为文本文件")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #f5f5f5;
                color: #333333;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #eeeeee;
                border-color: #d0d0d0;
            }
            QPushButton:pressed {
                background-color: #e0e0e0;
            }
        """)
        self.save_btn.clicked.connect(self.save_text)
        btn_layout.addWidget(self.save_btn)
        
        right_layout.addLayout(btn_layout)
        content_layout.addLayout(right_layout, 1)
        main_layout.addLayout(content_layout)
        
        # 设置全局字体
        font = QFont()
        font.setFamily("Microsoft YaHei")  # 微软雅黑，更美观的中文字体
        font.setPointSize(12)
        self.setFont(font)
        
        # 创建应用图标
        self.app_icon = self.create_app_icon()
        self.setWindowIcon(self.app_icon)
        
        # 创建系统托盘
        self.create_tray_icon()
        self.show()

    # 重写paintEvent实现窗口圆角
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 12, 12)
        painter.setClipPath(path)
        super().paintEvent(event)

    def create_app_icon(self):
        icon_img = create_ocr_icon()
        data = icon_img.tobytes("raw", "RGBA")
        qimg = QImage(data, icon_img.width, icon_img.height, QImage.Format_RGBA8888)
        pixmap = QPixmap.fromImage(qimg)
        return QIcon(pixmap)

    def create_tray_icon(self):
        tray_icon = self.app_icon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(tray_icon)
        self.tray_icon.setToolTip("截屏OCR工具")
        tray_menu = QMenu()
        show_action = QAction("显示窗口", self)
        quit_action = QAction("退出", self)
        # 美化托盘菜单
        show_action.setFont(QFont("Microsoft YaHei", 11))
        quit_action.setFont(QFont("Microsoft YaHei", 11))
        show_action.triggered.connect(self.show_window)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            bring_window_to_front(int(self.winId()))

    def quit_app(self):
        QApplication.quit()

    def closeEvent(self, event):
        if self.tray_icon.isVisible():
            QMessageBox.information(self, "提示", "程序已最小化到系统托盘。\n右键托盘图标可退出。")
            self.hide()
            event.ignore()
        else:
            event.accept()

    def start_screenshot(self):
        self.hide()
        QApplication.processEvents()
        QTimer.singleShot(200, self._launch_screenshot)

    def _launch_screenshot(self):
        self.screenshot_widget = ScreenshotWidget()
        self.screenshot_widget.screenshot_taken.connect(self.on_ocr_ready)
        self.screenshot_widget.show()

    def on_ocr_ready(self, pil_image):
        self.current_screenshot = pil_image  # 保存截图用于显示

        self.text_edit.setPlainText("正在识别，请稍候...")
        self.image_label.setText("识别中...")
        QApplication.processEvents()
        
        try:
            if pil_image.width == 0 or pil_image.height == 0:
                QMessageBox.warning(self, "提示", "截图区域无效，请重试。")
                self.show_window()
                return

            # 图片缩放优化
            max_width = 1280
            max_height = 720
            w, h = pil_image.size
            if w > max_width or h > max_height:
                scale = min(max_width/w, max_height/h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                pil_image = pil_image.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # 保存临时文件
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                temp_path = tmp.name
                pil_image.save(temp_path, "PNG")

            # 启动异步 OCR
            self.ocr_thread = QThread()
            self.ocr_worker = OCRWorker(temp_path)
            self.ocr_worker.moveToThread(self.ocr_thread)
            
            self.ocr_thread.started.connect(self.ocr_worker.run)
            self.ocr_worker.result_ready.connect(self.handle_ocr_result)
            self.ocr_worker.error_occurred.connect(self.handle_ocr_error)
            self.ocr_worker.result_ready.connect(self.ocr_thread.quit)
            self.ocr_worker.error_occurred.connect(self.ocr_thread.quit)
            self.ocr_thread.finished.connect(lambda: os.unlink(temp_path))  # 自动清理
            self.ocr_thread.finished.connect(self.ocr_thread.deleteLater)
            
            self.ocr_thread.start()

        except Exception as e:
            QMessageBox.critical(self, "预处理失败", str(e))
            self.show_window()

    def select_image_for_ocr(self):
        # 打开文件对话框选择图片，支持常见图片格式
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片文件",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.tiff);;所有文件 (*.*)"
        )
        if not file_path:
            return  # 用户取消选择
        
        try:
            # 加载本地图片
            pil_image = Image.open(file_path)
            # 保存图片引用（复用现有显示逻辑）
            self.current_screenshot = pil_image
            
            self.text_edit.setPlainText("正在识别，请稍候...")
            self.image_label.setText("识别中...")
            QApplication.processEvents()
            
            # 复用图片缩放优化逻辑，保证识别效率
            max_width = 1280
            max_height = 720
            w, h = pil_image.size
            if w > max_width or h > max_height:
                scale = min(max_width/w, max_height/h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                pil_image = pil_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            
            # 保存为临时文件（和截图逻辑一致）
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                temp_path = tmp.name
                pil_image.save(temp_path, "PNG")
            
            # 复用异步OCR线程逻辑，保证代码一致性
            self.ocr_thread = QThread()
            self.ocr_worker = OCRWorker(temp_path)
            self.ocr_worker.moveToThread(self.ocr_thread)
            
            self.ocr_thread.started.connect(self.ocr_worker.run)
            self.ocr_worker.result_ready.connect(self.handle_ocr_result)
            self.ocr_worker.error_occurred.connect(self.handle_ocr_error)
            self.ocr_worker.result_ready.connect(self.ocr_thread.quit)
            self.ocr_worker.error_occurred.connect(self.ocr_thread.quit)
            self.ocr_thread.finished.connect(lambda: os.unlink(temp_path))
            self.ocr_thread.finished.connect(self.ocr_thread.deleteLater)
            
            self.ocr_thread.start()
            
        except Exception as e:
            QMessageBox.critical(self, "图片加载失败", f"无法加载所选图片：{str(e)}")

    def handle_ocr_result(self, full_text):
        try:
            pil_img = self.current_screenshot
            if pil_img.mode != "RGBA":
                pil_img = pil_img.convert("RGBA")
            data = pil_img.tobytes("raw", "RGBA")
            qimg = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGBA8888)
            pixmap = QPixmap.fromImage(qimg).scaled(
                self.image_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.image_label.setPixmap(pixmap)
            self.text_edit.setPlainText(full_text)
        except Exception as e:
            QMessageBox.critical(self, "显示错误", str(e))
        finally:
            self.show_window()

    def handle_ocr_error(self, error_msg):
        QMessageBox.critical(self, "OCR 识别失败", error_msg)
        self.show_window()

    def copy_text(self):
        pyperclip.copy(self.text_edit.toPlainText())
        QMessageBox.information(self, "提示", "已复制到剪贴板！", QMessageBox.Ok, QMessageBox.Ok)

    def save_text(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存", "", "文本文件 (*.txt)")
        if path:
            if not path.endswith('.txt'):
                path += '.txt'
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.text_edit.toPlainText())
            QMessageBox.information(self, "成功", f"已保存到：\n{path}")


# ========== 启动程序 ==========
if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # 检查引擎是否存在
    engine_path = get_engine_path()
    models_dir = get_models_dir()
    missing = []
    if not os.path.exists(engine_path):
        missing.append(engine_path)
    required_models = [
        "ppocr_keys_v1.txt",
        "ch_PP-OCRv4_det_infer.onnx",
        "ch_ppocr_mobile_v2.0_cls_infer.onnx"，
        "ch_PP-OCRv4_rec_infer.onnx"
    ]
    for m in required_models:
        if not os.path.exists(os.path.join(models_dir, m)):
            missing.append(os.path.join(models_dir, m))
    
    if missing:
        QMessageBox.critical(None, "错误", f"以下文件缺失：\n" + "\n".join(missing))
        sys.exit(1)
    
    window = OCRMainWindow()
    sys.exit(app.exec())
