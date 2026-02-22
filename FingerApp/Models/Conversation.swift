import Foundation
import SwiftData

@Model
class Conversation {
    @Attribute(.unique) var id: UUID = UUID()
    var title: String = "New Chat"
    var createdAt: Date = Date()
    var updatedAt: Date = Date()
    var sessionID: String = UUID().uuidString

    @Relationship(deleteRule: .cascade, inverse: \Message.conversation)
    var messages: [Message] = []

    /// Messages sorted by timestamp for display
    var sortedMessages: [Message] {
        messages.sorted { $0.timestamp < $1.timestamp }
    }

    /// Auto-generate title from first user message
    func generateTitle() {
        guard let first = messages.first(where: { $0.role == "user" }) else { return }
        let preview = String(first.text.prefix(40))
        title = preview + (first.text.count > 40 ? "..." : "")
    }
}
