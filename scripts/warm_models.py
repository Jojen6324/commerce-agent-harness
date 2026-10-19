"""Warm the original model IDs at the revisions verified during onboarding."""

from sentence_transformers import CrossEncoder, SentenceTransformer


def main():
    embedding = SentenceTransformer(
        "BAAI/bge-small-zh-v1.5", revision="7999e1d3359715c523056ef9478215996d62a620", device="cpu"
    )
    assert embedding.get_sentence_embedding_dimension() == 512
    assert len(embedding.encode(["退货政策"])[0]) == 512
    rerank = CrossEncoder(
        "BAAI/bge-reranker-base",
        revision="2cfc18c9415c912f9d8155881c133215df768a70",
        device="cpu",
        max_length=512,
    )
    scores = rerank.predict(
        [("质量问题能退吗", "质量问题可申请售后"), ("质量问题能退吗", "配送需要三天")]
    )
    assert scores[0] > scores[1]
    print("BGE embeddings and Cross-Encoder inference verified on CPU.")


if __name__ == "__main__":
    main()
