import Speech
import AVFoundation

@Observable
class SpeechManager {
    var isRecording = false
    var transcript = ""

    private var audioEngine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    func startRecording() {
        guard !isRecording else { return }

        SFSpeechRecognizer.requestAuthorization { status in
            guard status == .authorized else { return }
            Task { @MainActor in
                self.beginRecording()
            }
        }
    }

    private func beginRecording() {
        let recognizer = SFSpeechRecognizer()
        guard let recognizer, recognizer.isAvailable else { return }

        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true
        self.request = req

        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.record, mode: .measurement)
        try? session.setActive(true)

        let engine = AVAudioEngine()
        self.audioEngine = engine
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            req.append(buffer)
        }

        engine.prepare()
        try? engine.start()
        isRecording = true
        transcript = ""

        recognitionTask = recognizer.recognitionTask(with: req) { [weak self] result, _ in
            guard let result else { return }
            Task { @MainActor in
                self?.transcript = result.bestTranscription.formattedString
            }
        }
    }

    func stopRecording() {
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil
        request = nil
        audioEngine = nil
        isRecording = false
    }
}
