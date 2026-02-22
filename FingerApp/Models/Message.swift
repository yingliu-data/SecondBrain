import Foundation
import SwiftData

@Model
class Message {
    @Attribute(.unique) var id: UUID = UUID()
    var role: String = "user"
    var text: String = ""
    var timestamp: Date = Date()
    var conversation: Conversation?

    init(role: String, text: String, conversation: Conversation? = nil) {
        self.id = UUID()
        self.role = role
        self.text = text
        self.timestamp = Date()
        self.conversation = conversation
    }
}
