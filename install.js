module.exports = {
  run: [{
    method: "shell.run",
    params: {
      path: "app",
      message: "python -m venv env"
    }
  }, {
    method: "shell.run",
    params: {
      venv: "app/env",
      path: ".",
      message: [
        "uv pip install -r backend/requirements.txt",
        "npm --prefix frontend install",
        "npm --prefix frontend run build"
      ]
    }
  }, {
    method: "notify",
    params: {
      html: "Installation Complete! Click 'Start' to launch the application."
    }
  }]
}
