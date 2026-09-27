# storage-path-bench

Measure how long a model's bytes take to reach RAM from a given path, and record the conditions that make the number comparable: mount type and options, NFS connection count, readahead, whether the client cache was dropped, load before the run.

```sh
sudo ./spb.py run --path /srv/models/llm/<org>/<name>/<commit> --label nfs-cold  --rounds 3 --drop-caches
     ./spb.py run --path /srv/models/llm/<org>/<name>/<commit> --label nfs-warm  --rounds 3
sudo ./spb.py run --path /nvme/models/<same entry>              --label nvme-cold --rounds 3 --drop-caches
     ./spb.py run --path /srv/models/gguf/<...>                  --label nfs-mmap  --mode mmap
     ./spb.py report results.jsonl
```

`--mode read` streams every file with 8 MiB reads (what safetensors loaders and vLLM do). `--mode mmap` touches one byte per page (what llama.cpp and Ollama do with GGUF). `--drop-caches` needs root and clears only the client's page cache; the NAS keeps its ARC, and the JSONL records that the server cache state is unknown. To get a server-cold number, read a file set larger than the NAS's RAM first, or reboot the NAS.

Standard library only. Apache-2.0.
