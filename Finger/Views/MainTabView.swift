import SwiftUI

enum Tab: String, CaseIterable {
    case chat, skills, settings

    var icon: String {
        switch self {
        case .chat: "message.fill"
        case .skills: "bolt.fill"
        case .settings: "gearshape.fill"
        }
    }

    var label: String {
        switch self {
        case .chat: "Chat"
        case .skills: "Skills"
        case .settings: "Settings"
        }
    }
}

struct MainTabView: View {
    @State private var selectedTab: Tab = .chat

    var body: some View {
        ZStack {
            AppTheme.backgroundGradient.ignoresSafeArea()

            VStack(spacing: 0) {
                Group {
                    switch selectedTab {
                    case .chat: ChatView()
                    case .skills: SkillsView()
                    case .settings: SettingsView()
                    }
                }
                .frame(maxHeight: .infinity)

                // Frosted glass tab bar
                HStack {
                    ForEach(Tab.allCases, id: \.self) { tab in
                        Button {
                            withAnimation(.spring(duration: 0.3)) {
                                selectedTab = tab
                            }
                        } label: {
                            VStack(spacing: 4) {
                                Image(systemName: tab.icon)
                                    .font(.system(size: 20))
                                Text(tab.label)
                                    .font(.caption2)
                            }
                            .foregroundColor(selectedTab == tab ? .white : AppTheme.textSecondary)
                            .scaleEffect(selectedTab == tab ? 1.1 : 1.0)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                            .background(
                                Group {
                                    if selectedTab == tab {
                                        RoundedRectangle(cornerRadius: AppTheme.radiusSm)
                                            .fill(.white.opacity(0.2))
                                    }
                                }
                            )
                        }
                    }
                }
                .padding(.horizontal)
                .padding(.top, 8)
                .padding(.bottom, 4)
                .background(AppTheme.glassHeavy)
                .background(.ultraThinMaterial)
                .overlay(alignment: .top) {
                    Rectangle()
                        .fill(AppTheme.border)
                        .frame(height: 0.5)
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}

#Preview {
    MainTabView()
}
