import SwiftUI

struct SettingsView: View {
    @State private var showDebugLogs = false
    private var debugLog = DebugLog.shared

    var body: some View {
        GlassEffectContainer {
        ScrollView {
            VStack(spacing: 20) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Image(systemName: "gearshape.fill")
                            .font(.title2)
                            .foregroundColor(.white)
                        Text("Settings")
                            .font(.title2.weight(.semibold))
                            .foregroundColor(.white)
                    }
                    Text("Customize your experience")
                        .font(.subheadline)
                        .foregroundColor(AppTheme.textTertiary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))

                SettingsSection(title: "Preferences") {
                    SettingsRow(icon: "waveform", label: "Wake Word", detail: "Off")
                    SettingsRow(icon: "speaker.wave.2.fill", label: "Voice Speed", detail: "Normal")
                }

                SettingsSection(title: "About") {
                    SettingsRow(icon: "info.circle", label: "Version", detail: "0.7.0")
                    SettingsRow(icon: "doc.text", label: "License", detail: "MIT")
                }

                SettingsSection(title: "Developer") {
                    SettingsRow(
                        icon: "ladybug",
                        label: "Debug Logs",
                        detail: showDebugLogs ? "On" : "Off"
                    )
                    .onTapGesture { withAnimation { showDebugLogs.toggle() } }
                }

                if showDebugLogs {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("LOGS")
                                .font(.caption.weight(.medium))
                                .foregroundColor(AppTheme.textTertiary)
                            Spacer()
                            Button("Clear") { debugLog.clear() }
                                .font(.caption)
                                .foregroundColor(AppTheme.accent)
                        }
                        .padding(.horizontal, 4)

                        if debugLog.entries.isEmpty {
                            Text("No logs yet. Send a message to see debug output.")
                                .font(.caption)
                                .foregroundColor(AppTheme.textTertiary)
                                .padding(12)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusMd))
                        } else {
                            VStack(alignment: .leading, spacing: 2) {
                                ForEach(Array(debugLog.entries.reversed().enumerated()), id: \.offset) { _, entry in
                                    Text(entry)
                                        .font(.system(.caption2, design: .monospaced))
                                        .foregroundColor(AppTheme.textSecondary)
                                        .textSelection(.enabled)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusMd))
                        }
                    }
                }
            }
            .padding(16)
        }
        .background { AppTheme.wallpaper.resizable().aspectRatio(contentMode: .fill).ignoresSafeArea() }
        } // GlassEffectContainer
    }
}

private struct SettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption.weight(.medium))
                .foregroundColor(AppTheme.textTertiary)
                .padding(.horizontal, 4)

            VStack(spacing: 0) {
                content
            }
            .glassEffect(in: RoundedRectangle(cornerRadius: AppTheme.radiusLg))
        }
    }
}

private struct SettingsRow: View {
    let icon: String
    let label: String
    var detail: String? = nil

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.body)
                .foregroundColor(AppTheme.accent)
                .frame(width: 36, height: 36)
                .glassEffect(in: RoundedRectangle(cornerRadius: 10))

            Text(label)
                .font(.body)
                .foregroundColor(.white)
            Spacer()
            if let detail {
                Text(detail)
                    .font(.subheadline)
                    .foregroundColor(AppTheme.textTertiary)
            }
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundColor(AppTheme.textTertiary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) {
            Rectangle().fill(AppTheme.border).frame(height: 0.5).padding(.leading, 60)
        }
    }
}
