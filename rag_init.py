import chromadb
from sentence_transformers import SentenceTransformer

# 模擬從 Markdown 筆記中切出的三個區塊。
documents = [
	"區塊 A：聯電（２３０３）今天會大漲10%。",
	"區塊 B：今天晚上要臭豆腐跟蚵仔煎",
	"區塊 C：台灣加權指數今天會平盤收尾",
]
def main() -> None:
	# 使用記憶體模式；程式結束後資料會清空。
	client = chromadb.Client()
	collection = client.create_collection(name="my_notes")

	# model = SentenceTransformer("BAAI/bge-m3")
	# document_embeddings = model.encode(documents, normalize_embeddings=True).tolist()

	collection.add(
		ids=["chunk-a", "chunk-b", "chunk-c"],
		documents=documents
		# embeddings=document_embeddings,
	)
	print(f"已加入 {collection.count()} 個 chunks。")

	chunks = collection.get(include=["documents"])
	print("目前的 chunks：")
	for chunk_id, document in zip(chunks["ids"], chunks["documents"], strict=True):
		print(f"{chunk_id}: {document}")
	print("*" * 40)

	query = "今天股票市場會怎麼走？"
	# query_embedding = model.encode([query], normalize_embeddings=True).tolist()
	results = collection.query(
		query_texts=[query],
		# query_embeddings=query_embedding,
		n_results=3,
	)

	print(f"\n查詢：{query}")
	print("最相近的 chunks：")
	for document, distance in zip(
		results["documents"][0], results["distances"][0], strict=True
	):
		print(f"- distance={distance:.4f}: {document}")
	print("*" * 40)


if __name__ == "__main__":
	main()
