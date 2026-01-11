截屏OCR及图片OCR程序（无需联网可使用）

OCR调用RapidOcrOnnx.exe（来自https://github.com/RapidAI/RapidOcrOnnx）

使用Rapid的检测识别OCRv4模型（来自https://github.com/RapidAI/RapidOCR）

主要是学习如何生成托盘图标常驻运行（如要用作生产工具建议使用Umi-OCR）

依赖
PySide6 mss Pillow pyperclip


程序文件夹

├── ocr.py  

├── ocr_icon.ico  

└── rapidocr/

    ├── RapidOcrOnnx.exe
    
    └── models/        
    
         ├──ppocr_keys_v1.txt
         
         ├──ch_PP-OCRv4_rec_infer.onnx
         
         ├──ch_PP-OCRv4_det_infer.onnx
         
         └──ch_ppocr_mobile_v2.0_cls_infer.onnx
