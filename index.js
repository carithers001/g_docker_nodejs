const express = require("express");
const path = require("path");
const app = express();

// ¿¿¿¿¿¿¿¿¿ 3000¿¿¿¿ Dockerfile ¿¿¿¿
const PORT = process.env.PORT || 3000;

// ¿¿¿¿¿¿¿¿ (/) ¿¿¿¿¿¿¿ index.html ¿¿
app.get("/", (req, res) => {
  const filePath = path.join(__dirname, 'index.html');
  res.sendFile(filePath, (err) => {
    if (err) {
      console.error("¿¿¿¿ index.html:", err);
      res.status(500).send("¿¿¿ index.html ¿¿¿");
    }
  });
});

// ¿¿¿¿¿¿¿¿¿¿
app.listen(PORT, () => {
  console.log(`¿ ¿¿¿¿¿¿¿¿¿`);
  console.log(`¿ ¿¿¿¿¿¿: ${PORT}`);
});
