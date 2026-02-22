import SwiftUI

enum AppTheme {
    static let backgroundGradient = LinearGradient(
        colors: [Color(hex: "0f172a"), Color(hex: "1e293b"), Color(hex: "171717")],
        startPoint: .top, endPoint: .bottom
    )

    static let accentGradient = LinearGradient(
        colors: [Color(hex: "10b981"), Color(hex: "0d9488")],
        startPoint: .leading, endPoint: .trailing
    )
    static let emerald = Color(hex: "10b981")

    static let textPrimary = Color.white
    static let textSecondary = Color.white.opacity(0.6)
    static let textTertiary = Color.white.opacity(0.4)

    static let border = Color.white.opacity(0.1)

    static let radiusLg: CGFloat = 24
    static let radiusMd: CGFloat = 16
    static let radiusSm: CGFloat = 12
}

extension Color {
    init(hex: String) {
        let scanner = Scanner(string: hex)
        var rgb: UInt64 = 0
        scanner.scanHexInt64(&rgb)
        self.init(
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255
        )
    }
}
