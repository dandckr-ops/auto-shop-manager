const loginStatus = document.querySelector("#loginStatus");
const loginForm = document.querySelector("#loginForm");
const keycloakLogin = document.querySelector("#keycloakLogin");
const params = new URLSearchParams(window.location.search);
const nextPath = params.get("next") || "/";

function setLoginStatus(message, isError = false) {
  loginStatus.textContent = message || "";
  loginStatus.classList.toggle("error-text", isError);
}

async function loadPublicAuthConfig() {
  try {
    const response = await fetch("/api/auth/public", { cache: "no-store" });
    const config = await response.json();
    loginForm.hidden = !config.localEnabled;
    keycloakLogin.hidden = !config.oidcEnabled;
    keycloakLogin.href = `/auth/keycloak?next=${encodeURIComponent(nextPath)}`;
    if (!config.localEnabled && !config.oidcEnabled) {
      window.location.href = nextPath;
    }
  } catch {
    setLoginStatus("Unable to load auth settings.", true);
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setLoginStatus("Signing in...");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#loginUsername").value.trim(),
        password: document.querySelector("#loginPassword").value,
        next: nextPath
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "Login failed");
    }
    window.location.href = payload.next || nextPath;
  } catch (error) {
    setLoginStatus(error.message, true);
  }
});

if (params.get("error")) {
  setLoginStatus(params.get("error"), true);
}

loadPublicAuthConfig();
