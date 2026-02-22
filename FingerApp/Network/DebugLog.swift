import Foundation

@Observable
class DebugLog {
    static let shared = DebugLog()
    var entries: [String] = []

    func log(_ message: String) {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        let timestamp = formatter.string(from: Date())
        entries.append("[\(timestamp)] \(message)")
        if entries.count > 200 {
            entries.removeFirst(entries.count - 200)
        }
    }

    func clear() {
        entries.removeAll()
    }
}
