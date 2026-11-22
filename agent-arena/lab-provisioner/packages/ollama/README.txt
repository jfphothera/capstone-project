The pinned Ollama for the lab heads (provisioner 1.6.14): ollama-windows-amd64.zip, Ollama 0.40.15.

Official archive from https://github.com/ollama/ollama/releases/tag/v0.40.15, checked by the build and again on the head
against the SHA-256 in config\lab-network.json "ollama"."runtime" (9ca74a7ab66ec86870b1eecac7df160df08dc4e3a291ab5d2d59bcf9310221c6),
and ollama.exe against its Authenticode publisher (Ollama Inc.) before use. HEAD-OLLAMA.cmd unpacks it into
C:\ProgramData\AgentArenaLab\ollama\runtime\0.40.15 beside the old install (kept for rollback). No internet is needed.
Models are never bundled: they are pulled in their own stage.

Optional, older path: OllamaSetup.exe here is used only by a first-time head install when the archive above is absent.
