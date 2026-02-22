import SwiftUI

enum AppTheme {
    static let wallpaper = Image("Wallpaper")

    static let accentGradient = LinearGradient(
        colors: [Color(hex: "0A84FF"), Color(hex: "0066CC")],
        startPoint: .leading, endPoint: .trailing
    )
    static let accent = Color.accentColor

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
