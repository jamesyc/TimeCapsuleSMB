import SwiftUI

struct RevealablePasswordField: View {
    let placeholder: String
    @Binding var text: String
    let onSubmit: () -> Void

    @State private var isPasswordVisible = false

    init(
        _ placeholder: String,
        text: Binding<String>,
        onSubmit: @escaping () -> Void = {}
    ) {
        self.placeholder = placeholder
        _text = text
        self.onSubmit = onSubmit
    }

    var body: some View {
        HStack(spacing: 6) {
            Group {
                if isPasswordVisible {
                    TextField(placeholder, text: $text)
                } else {
                    SecureField(placeholder, text: $text)
                }
            }
            .onSubmit(onSubmit)

            Button {
                isPasswordVisible.toggle()
            } label: {
                Image(systemName: isPasswordVisible ? "eye.slash" : "eye")
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.borderless)
            .help(visibilityTitle)
            .accessibilityLabel(visibilityTitle)
        }
        .onChange(of: text) { _, newValue in
            if newValue.isEmpty {
                isPasswordVisible = false
            }
        }
    }

    private var visibilityTitle: String {
        L10n.string(isPasswordVisible ? "password.hide" : "password.show")
    }
}
