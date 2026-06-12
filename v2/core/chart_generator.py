# core/chart_generator.py
# 텔레그램용 미니 차트 생성
# AI 분석용 캔들스틱 차트 제거 (Ver5.1: 이미지 입력 폐지)
# 메모리 최적화 필수 (GCP e2-small 2GB)

import io
import gc
from typing import Optional

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from config import get_logger

logger = get_logger("chart_generator")


def generate_mini_chart(df: pd.DataFrame) -> bytes:
    """
    간소화된 미니 차트 (텔레그램용)
    """
    df = df.tail(50).copy()
    
    fig = None
    buffer = None
    
    try:
        fig, ax = plt.subplots(figsize=(8, 4), dpi=80)
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#1a1a2e")
        
        x = range(len(df))
        ax.plot(x, df["close"].values, color="#26a69a", linewidth=1.5)
        ax.fill_between(x, df["close"].values, alpha=0.3, color="#26a69a")
        
        ax.set_title("BTC/USDT", color="#ffffff", fontsize=10)
        ax.tick_params(axis="both", colors="#ffffff", labelsize=7)
        ax.grid(True, alpha=0.2, color="#ffffff")
        
        plt.tight_layout()
        
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", bbox_inches="tight", facecolor="#1a1a2e")
        image_bytes = buffer.getvalue()
        
        return image_bytes
    
    finally:
        if buffer:
            buffer.close()
        
        plt.cla()
        plt.clf()
        
        if fig:
            plt.close(fig)
        
        plt.close("all")
        del df
        gc.collect()