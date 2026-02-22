import Foundation
import CryptoKit
import UIKit

@Observable
class AssistantClient {
    var isProcessing = false
    var currentResponse = ""

    private let serverURL = "https://secondbrain.yingliu.site"
    private let apiKey = "5af0bca7f3d77b4383b4d641264cdcb364a5386c7599dc0bdb3a9e779c2a368e"
    private let cfClientId = "5ebfb44ae04fc56ec481d333aba90b29.access"
    private let cfClientSecret = "6e98e4adb9ff94cc9d5c0942de1a6c3c52db1572db6e7dfa878f029c7aceca1c"

    // MARK: - Chat

    func send(message: String, sessionID: String) async {
        let debug = DebugLog.shared
        await MainActor.run {
            isProcessing = true
            currentResponse = ""
        }

        let url = URL(string: "\(serverURL)/api/v1/chat")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(cfClientId, forHTTPHeaderField: "CF-Access-Client-Id")
        request.setValue(cfClientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")

        let body = try! JSONEncoder().encode(["message": message, "session_id": sessionID] as [String: String])
        request.httpBody = body
        signRequest(&request, body: body)

        debug.log("POST \(url.absoluteString)")
        debug.log("Headers: CF-Access-Client-Id=\(cfClientId.prefix(8))…, Authorization=Bearer \(apiKey.prefix(8))…")

        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            let http = response as? HTTPURLResponse
            debug.log("Response status: \(http?.statusCode ?? -1)")

            if let http, http.statusCode != 200 {
                var errorBody = ""
                for try await line in bytes.lines { errorBody += line + "\n" }
                let snippet = String(errorBody.prefix(500))
                debug.log("Error body: \(snippet)")
                await MainActor.run {
                    currentResponse = "HTTP \(http.statusCode): \(String(snippet.prefix(200)))"
                }
                return
            }

            debug.log("SSE stream connected")
            var currentEvent = ""

            for try await line in bytes.lines {
                if line.hasPrefix("event: ") {
                    currentEvent = String(line.dropFirst(7))
                    continue
                }

                guard line.hasPrefix("data: ") else { continue }
                let jsonStr = String(line.dropFirst(6))

                guard let data = jsonStr.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else { continue }

                switch currentEvent {
                case "token":
                    if let text = obj["text"] as? String {
                        await MainActor.run { currentResponse += text }
                    }

                case "tool_call":
                    let tc = ToolCallRequest(
                        id: obj["id"] as? String ?? "",
                        name: obj["name"] as? String ?? "",
                        arguments: obj["arguments"] as? [String: Any] ?? [:]
                    )
                    debug.log("Tool call: \(tc.name)")
                    await executeToolLocally(tc)

                case "done":
                    debug.log("Stream done")

                default:
                    break
                }
            }
        } catch {
            debug.log("Connection error: \(error.localizedDescription)")
            await MainActor.run {
                currentResponse = "Connection error: \(error.localizedDescription)"
            }
        }

        await MainActor.run { isProcessing = false }
    }

    // MARK: - Tool Execution

    func executeToolLocally(_ call: ToolCallRequest) async {
        let result: String
        switch call.name {
        case "get_calendar_events":
            result = await CalendarTool.getEvents(daysAhead: call.arguments["days_ahead"] as? Int ?? 7)
        case "create_calendar_event":
            result = await CalendarTool.createEvent(
                title: call.arguments["title"] as? String ?? "Untitled",
                startDate: call.arguments["start_date"] as? String ?? "",
                duration: call.arguments["duration_minutes"] as? Int ?? 60
            )
        case "get_reminders":
            result = await RemindersTool.getPending()
        case "create_reminder":
            result = await RemindersTool.create(
                title: call.arguments["title"] as? String ?? "Untitled",
                dueDate: call.arguments["due_date"] as? String
            )
        case "search_contacts":
            result = await ContactsTool.search(name: call.arguments["name"] as? String ?? "")
        case "read_clipboard":
            result = await MainActor.run { UIPasteboard.general.string ?? "Clipboard is empty." }
        default:
            result = "Unknown tool: \(call.name)"
        }
        await sendToolResult(callID: call.id, result: result)
    }

    // MARK: - Tool Result

    func sendToolResult(callID: String, result: String) async {
        let debug = DebugLog.shared
        let url = URL(string: "\(serverURL)/api/v1/tool-result")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(cfClientId, forHTTPHeaderField: "CF-Access-Client-Id")
        request.setValue(cfClientSecret, forHTTPHeaderField: "CF-Access-Client-Secret")

        let body = try! JSONEncoder().encode([
            "tool_call_id": callID,
            "result": result,
        ])
        request.httpBody = body
        signRequest(&request, body: body)

        debug.log("POST tool-result id=\(callID)")
        if let (_, response) = try? await URLSession.shared.data(for: request),
           let http = response as? HTTPURLResponse {
            debug.log("Tool result response: \(http.statusCode)")
        }
    }

    // MARK: - Auth (HMAC-SHA256)

    private func signRequest(_ request: inout URLRequest, body: Data) {
        let timestamp = String(Int(Date().timeIntervalSince1970))
        let payload = timestamp + String(data: body, encoding: .utf8)!
        let key = SymmetricKey(data: Data(apiKey.utf8))
        let signature = HMAC<SHA256>.authenticationCode(for: Data(payload.utf8), using: key)
            .map { String(format: "%02x", $0) }
            .joined()

        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue(timestamp, forHTTPHeaderField: "X-Timestamp")
        request.setValue(signature, forHTTPHeaderField: "X-Signature")
    }
}

struct ToolCallRequest {
    let id: String
    let name: String
    let arguments: [String: Any]
}
