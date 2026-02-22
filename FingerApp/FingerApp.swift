import SwiftUI
import SwiftData

@main
struct FingerApp: App {
    var body: some Scene {
        WindowGroup {
            MainTabView()
        }
        .modelContainer(for: Conversation.self)
    }
}
