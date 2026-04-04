function resolveUploadEndpoints() {
    const endpoints = [];
    const configuredBase = (window.GST_API_BASE_URL || "").trim();
    if (configuredBase) {
        endpoints.push(`${configuredBase.replace(/\/+$/, "")}/upload`);
    }

    if (window.location.protocol.startsWith("http")) {
        endpoints.push(`${window.location.origin.replace(/\/+$/, "")}/upload`);
    }

    endpoints.push("https://gst-smartcheck.onrender.com/upload");
    return [...new Set(endpoints)];
}

async function postToUpload(formData) {
    const endpoints = resolveUploadEndpoints();
    let lastError = null;

    for (const endpoint of endpoints) {
        try {
            return await fetch(endpoint, {
                method: "POST",
                body: formData,
            });
        } catch (error) {
            lastError = error;
        }
    }

    throw lastError || new Error("Unable to connect to any upload endpoint.");
}

document.getElementById("uploadForm").addEventListener("submit", async function (e) {
    e.preventDefault();

    const status = document.getElementById("statusMessage");
    status.innerText = "Processing...";

    const email = document.getElementById("email").value;
    const file = document.getElementById("invoiceFile").files[0];
    const selectedOutput = document.querySelector('input[name="outputFormat"]:checked');

    const formData = new FormData();
    formData.append("email", email);
    formData.append("file", file);
    formData.append("output_format", selectedOutput ? selectedOutput.value : "xlsx");

    try {
        const response = await postToUpload(formData);

        if (!response.ok) {
            let errorMessage = `Request failed (${response.status}).`;
            const contentType = (response.headers.get("content-type") || "").toLowerCase();

            if (contentType.includes("application/json")) {
                const errorData = await response.json();
                errorMessage = errorData.error || errorData.detail || errorMessage;
            } else {
                const rawError = (await response.text()).trim();
                if (rawError) {
                    errorMessage = rawError.slice(0, 220);
                }
            }

            status.innerText = `❌ ${errorMessage}`;
            return;
        }

        if (selectedOutput && selectedOutput.value === "xml") {
            const xmlBlob = await response.blob();
            const xmlUrl = window.URL.createObjectURL(xmlBlob);
            const downloadLink = document.createElement("a");
            downloadLink.href = xmlUrl;
            downloadLink.download = (file?.name || "invoice").replace(/\.[^.]+$/, "") + ".xml";
            document.body.appendChild(downloadLink);
            downloadLink.click();
            downloadLink.remove();
            window.URL.revokeObjectURL(xmlUrl);
            status.innerText = "✅ XML export ready.";
            return;
        }

        const payload = await response.json();
        const remaining = payload.remaining;
        const usageCount = payload.usage_count ?? 0;

        if (!payload.file_url) {
            status.innerText = "❌ Conversion completed but download link is missing.";
            return;
        }

        window.location.assign(payload.file_url);

        if (usageCount > 10) {
            status.innerText = "⚠️ Pro required for additional downloads.";
            return;
        }

        status.innerText = `✅ Success! Free uploads remaining: ${remaining}`;
    } catch (error) {
        status.innerText = `❌ Failed to reach backend. ${error?.message || "Check internet/API URL."}`;
    }
});
