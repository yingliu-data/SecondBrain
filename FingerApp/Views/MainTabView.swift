import SwiftUI

struct MainTabView: View {
    @State private var selectedConversation: Conversation?

    var body: some View {
        ZStack {
            AppTheme.wallpaper.resizable().aspectRatio(contentMode: .fill).ignoresSafeArea()
            TabView {
                Tab("Chat", systemImage: "message.fill") {
                    NavigationStack {
                        ConversationListView(selectedConversation: $selectedConversation)
                            .navigationDestination(item: $selectedConversation) { conversation in
                                ChatView(conversation: conversation)
                            }
                    }
                }
                Tab("Skills", systemImage: "bolt.fill") {
                    SkillsView()
                }
                Tab("Settings", systemImage: "gearshape.fill") {
                    SettingsView()
                }
            }
            .tint(AppTheme.accent)
        }
        .preferredColorScheme(.dark)
    }
}
