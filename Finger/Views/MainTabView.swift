import SwiftUI

struct MainTabView: View {
    var body: some View {
        TabView {
            Tab("Chat", systemImage: "message.fill") {
                ChatView()
            }
            Tab("Skills", systemImage: "bolt.fill") {
                SkillsView()
            }
            Tab("Settings", systemImage: "gearshape.fill") {
                SettingsView()
            }
        }
        .background {
            AppTheme.backgroundGradient.ignoresSafeArea()
        }
        .preferredColorScheme(.dark)
    }
}
