# Changelog

## 0.11.0 - 23 February 2026

### Server
- feat: LLM retry with exponential backoff (3 attempts, 1s/2s/4s) before cloud fallback
- feat: graceful SSE error when LLM is completely unavailable (no crash)
- feat: dcgm-exporter GPU monitoring container in docker-compose.yml

### iOS App
- feat: network retry with exponential backoff (3 attempts) for transient errors and 502/503/504
- feat: user-friendly error messages for auth failures, timeouts, and network issues
- feat: fresh HMAC signature per retry attempt (prevents stale timestamp rejections)

## 0.10.1 - 1 March 2026

### Server
- feat: email skill with IMAP sync (Gmail, Hotmail, improx), junk filtering, SQLite cache, FTS search, and SMTP reply

## 0.10.0 - 23 February 2026

### iOS App
- feat: Keychain credential storage replacing hardcoded API keys (server URL, API key, CF Access, ElevenLabs)
- feat: Settings UI with editable server URL, connection test button, and secure credential fields
- feat: first-launch detection opens Settings tab when credentials not configured
- feat: offline handling with health check before sending (graceful error messages)
- feat: setup-required banner in Settings when credentials are missing

### Server
- feat: SQLite session persistence replacing in-memory dict (survives container restart)
- feat: session CRUD endpoints (GET /api/v1/sessions, DELETE /api/v1/sessions/{id})
- feat: configurable SESSION_BACKEND ("sqlite" default, "memory" fallback)

## 0.9.0 - 23 February 2026

### iOS App
- feat: live transcription via ElevenLabs WebSocket streaming STT (real-time partial transcripts)
- feat: ElevenLabs TTS replacing AVSpeechSynthesizer for natural voice output
- fix: SSE stream hang when backend returns non-200 (defer isProcessing reset)
- refactor: code cleanup across all Swift files (remove dead code, fix force unwraps, tighten closures)

## 0.8.1 - 22 February 2026

### iOS App
- feat: text-to-speech (TTS) with AVSpeechSynthesizer for assistant responses
- feat: speaker toggle in Settings with persistent @AppStorage
- feat: TTS auto-speaks responses when speaker is enabled
- feat: TTS stops when user starts voice recording
- fix: audio session updated to .playAndRecord for STT/TTS compatibility
- feat: markdown stripping for cleaner TTS output

## 0.8.0 - 22 February 2026

### iOS App
- fix: keyboard dismissal with interactive scroll and tap gesture
- fix: multiline text input with auto-growing TextField(axis: .vertical)
- feat: scroll fade effect at top using iOS 26 scrollEdgeEffectStyle
- feat: multi-conversation support with SwiftData persistence
- feat: conversation list with create/switch between chats
- feat: NavigationStack with Liquid Glass back button
- feat: markdown rendering in assistant message bubbles
- feat: interactive choice buttons for LLM option responses
- feat: image upload "+" button placeholder (Coming Soon)
- feat: device vs server skill badges in Skills view

### Server
- fix: system prompt enforces shorter, plain-text responses
- fix: reduce max response tokens from 1024 to 512

## 0.7.1 - 22 February 2026

- Finger: Fix push-to-talk mic gesture (view identity preservation)
- Finger: Add debug log viewer in Settings → Developer
- Finger: Backend request/response logging via DebugLog

## 0.7.0 - 22 February 2026

- Finger: iOS 26 Liquid Glass effect on all surfaces (native .glassEffect API)
- Finger: Native TabView with auto Liquid Glass tab bar
- Finger: Add New Skill chatbot connected to real LLM
- Finger: Hardcoded credentials, removed server config from Settings UI
- Finger: Code cleanup across all views

## 0.6.0 - 22 February 2026

- Finger: Glass effect on all surfaces, Add New Skill chat flow, configurable server URL and API key
- Finger: Voice input via long-press mic button with on-device speech recognition

## 0.5.0 - 22 February 2026

- Finger: Dark-first UI with glass-morphism, emerald accents, tab bar, Skills and Settings views

## 0.4.1 - 22 February 2026

- Finger: Add required Info.plist privacy descriptions for Calendar, Reminders, and Contacts

## 0.4.0 - 21 February 2026

- Finger: On-device tool executors for Calendar, Reminders, Contacts, and Clipboard

## 0.3.0 - 21 February 2026

- Finger iOS client: SwiftUI chat app with HMAC auth, SSE streaming, and Cloudflare Access headers

## 0.2.0 - 21 February 2026

- Agent API: Modular architecture with skills system, native LLM function calling, and CRUD skills API

## 0.1.1 - 20 February 2026

- Agent API: Full agent loop with auth, tool calls, SSE streaming, and prompt injection defense

## 0.1.0 - 20 February 2026

- Infrastructure: Add GitHub Actions CI/CD deploy workflow, docker-compose, and agent-api stub
