# storage-path-bench

Measure how long a model's bytes take to reach RAM from a given path, and record the conditions that make the number comparable: mount type and options, NFS connection count, readahead, whether the client cache was dropped, load before the run.

```sh
sudo ./spb.py run --path /srv/models/llm/<org>/<name>/<commit> --label nfs-cold  --rounds 3 --drop-caches
     ./spb.py run --path /srv/models/llm/<org>/<name>/<commit> --label nfs-warm  --rounds 3
sudo ./spb.py run --path /nvme/models/<same entry>              --label nvme-cold --rounds 3 --drop-caches
     ./spb.py run --path /srv/models/gguf/<...>                  --label nfs-mmap  --mode mmap
     ./spb.py report results.jsonl
```

`--mode read` streams every file with 8 MiB reads (what safetensors loaders and vLLM do). `--mode mmap` touches one byte per page (what llama.cpp and Ollama do with GGUF). `--advise sequential` or `--advise willneed` adds an `madvise` hint to the mapping before the first touch. `--drop-caches` writes to `/proc/sys/vm/drop_caches` when run as root; without root it evicts only the files under `--path` with `posix_fadvise(DONTNEED)`, which also works on NFS. Either way it clears only the client's page cache; the NAS keeps its ARC, and the JSONL records that the server cache state is unknown. To get a server-cold number, read a file set larger than the NAS's RAM first, or reboot the NAS, and watch `zpool iostat` on the NAS while the run goes: if the disks are idle, the bytes came from the ARC.

Each row also records the readahead that applies to the path (the NFS mount's own bdi setting, or the whole disk's for a block device), the NFS connection count next to the requested `nconnect`, and the interface and negotiated link speed on the route to the NFS or iSCSI server. Automounted NFS paths are resolved to the real mount, not the autofs trigger.

Standard library only. Apache-2.0.
