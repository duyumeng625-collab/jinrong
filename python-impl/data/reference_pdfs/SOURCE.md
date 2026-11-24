# RAGEval-Chat reference PDFs

These PDFs were copied from the `data/` directory of [qiugongisme/RAGEval-Chat](https://github.com/qiugongisme/RAGEval-Chat) on 2026-10-28.

- 42 PDFs, about 8.9 MB total
- Original `laws_regulations/` and `policies_regulations/` subdirectories are preserved
- On first startup, the backend imports their text into the separate **RAGEval 金融法规示例文档** knowledge base and builds a persistent FAISS index
- Customer-facing pages do not serve the PDF originals; only matching reference excerpts are returned with a RAG answer
- PDF originals stay in this directory; generated metadata stays under `python-impl/data/`, and FAISS files follow `FAISS_INDEX_PATH` (default `python-impl/data/faiss/`)

The repository provides an MIT license, but the legal and policy documents can carry separate source notices. Check the original issuing authority's terms before redistributing these PDFs outside this project.
