module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        venv: "app/env",
        path: ".",
        message: "python -m backend.alexandria.main",
        on: [{
          // Capture the URL when the server prints it
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      }
    },
    {
      // Set the local variable 'url' for pinokio.js to display "Open Web UI"
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
