import chromadb
from sentence_transformers import SentenceTransformer

# 模擬從 Markdown 筆記中切出的三個區塊。
import os
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from usage_logger import log_usage

documents = [
	"聯電（２３０３）今天會大漲10%。",
	"今天晚上要臭豆腐跟蚵仔煎",
	"台灣加權指數今天會平盤收尾",
]

MODEL_NAME = "gemini-3.5-flash-lite"
USAGE_LOG_PATH = Path("rag_gemini_usage.jsonl")
INPUT_PRICE_PER_MILLION_USD = 0.30
OUTPUT_PRICE_PER_MILLION_USD = 2.50

class ChunkClassification(BaseModel):
	"""Gemini 對單一 chunk 的受限分類結果。"""

	label: Literal["股市狀況", "其他閒聊"] = Field(
		description="僅能是股市狀況或其他閒聊。"
	)

def classify_chunk(
	client: genai.Client,
	document: str,
	*,
	usage_step: str = "chunk_classification",
) -> ChunkClassification:
	"""使用 Gemini 對原始 chunk 產生結構化標籤。"""
	response = client.models.generate_content(
		model=MODEL_NAME,
		contents=(
			"請判斷下列文字的分類。只可選擇『股市狀況』或『其他閒聊』，"
			"並依指定 JSON schema 回傳。\n\n"
			f"文字：{document}"
		),
		config=types.GenerateContentConfig(
			response_mime_type="application/json",
			response_schema=ChunkClassification,
		),
	)
	log_usage(
		usage_step,
		response,
		model_name=MODEL_NAME,
		usage_log_path=USAGE_LOG_PATH,
		input_price_per_million=INPUT_PRICE_PER_MILLION_USD,
		output_price_per_million=OUTPUT_PRICE_PER_MILLION_USD,
	)

	if isinstance(response.parsed, ChunkClassification):
		return response.parsed
	return ChunkClassification.model_validate_json(response.text)

def main() -> None:
	api_key = os.environ.get("GEMINI_API_KEY")
	if not api_key:
		raise RuntimeError("請設定 GEMINI_API_KEY 環境變數。")

	gemini_client = genai.Client(api_key=api_key)
	classifications = [classify_chunk(gemini_client, document) for document in documents]
	tagged_documents = [
		f"{classification.label}：{document}"
		for document, classification in zip(documents, classifications, strict=True)
	]
	metadatas = [
		{
			"original_document": document,
			"classification": classification.label,
		}
		for document, classification in zip(documents, classifications, strict=True)
	]

	print("Gemini 自動化標籤結果：")
	for document, classification, tagged_document in zip(
		documents, classifications, tagged_documents, strict=True
	):
		print(f"- 原始：{document}")
		print(f"  分類：{classification.label}")
		print(f"  重組：{tagged_document}")
	print("*" * 60)

	# # 標籤與原始文字完成重組後，才建立供檢索使用的 BGE-M3 embedding。
	# embedding_model = SentenceTransformer("BAAI/bge-m3")
	# document_embeddings = embedding_model.encode(
	# 	tagged_documents,
	# 	normalize_embeddings=True,
	# ).tolist()

	# 使用記憶體模式；程式結束後資料會清空。
	client = chromadb.Client()
	collection = client.create_collection(name="my_notes")
	collection.add(
		ids=["chunk-a", "chunk-b", "chunk-c"],
		documents=tagged_documents,
		metadatas=metadatas
		# embeddings=document_embeddings,
	)
	print(f"已加入 {collection.count()} 個 chunks。")

	chunks = collection.get(include=["documents", "metadatas"])
	print("目前的 chunks：")
	for chunk_id, document, metadata in zip(
		chunks["ids"], chunks["documents"], chunks["metadatas"], strict=True
	):
		print(f"{chunk_id}: {document}")
		print(f"  保留原文：{metadata['original_document']}")
	print("*" * 60)

	query = "今天股票市場會怎麼走？"
	query_classification = classify_chunk(
		gemini_client,
		query,
		usage_step="query_classification",
	)
	print(f"查詢分類：{query_classification.label}")
	# query_embedding = embedding_model.encode(
	# 	[query],
	# 	normalize_embeddings=True,
	# ).tolist()
	results = collection.query(
		# query_embeddings=query_embedding,
		query_texts=[query],
		n_results=3,
		include=["documents", "metadatas", "distances"],
		where={"classification": query_classification.label},
	)

	print(f"\n查詢：{query}")
	print("最相近的 chunks：")
	for document, metadata, distance in zip(
		results["documents"][0],
		results["metadatas"][0],
		results["distances"][0],
		strict=True,
	):
		print(f"- distance={distance:.4f}: {document}")
		print(f"  原始文件：{metadata['original_document']}")
	print("*" * 60)


if __name__ == "__main__":
	main()
