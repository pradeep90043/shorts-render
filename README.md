# shorts-render

Cloud render and cloned-voice workflows for the CODE OR CAP automation. This repo holds **only** GitHub Actions workflows,
the helper scripts they call, and voice samples. The application itself lives in a separate private repository, which
dispatches these workflows through the GitHub API and downloads their artifacts.

| Workflow | Purpose |
|---|---|
| `render-short.yml` | Renders a Short from a bundle the app serves |
| `voicebox-scenes.yml` | Cloned-voice narration, one file per scene, fanned out over up to 20 runners |
| `voicebox-narration.yml` | Cloned-voice narration for one long script |
| `voicebox-tts.yml` | Single-runner cloned-voice speech |
| `omnivoice-scenes.yml` | Same contract as voicebox-scenes with the OmniVoice engine |
| `member-edit.yml` | Edits a member's uploaded video (trim, aspect, captions, watermark); the job arrives encrypted |
| `voicebox-image.yml` | Builds the Voicebox CPU image (the current image is already public on GHCR) |

Never commit secrets here. Voice samples are deliberately not in this repo: the speak and render jobs fetch voice samples, music, stickers and watermarks from the private app repository with the `SAMPLES_KEY` secret (a read-only deploy key for that repo).
