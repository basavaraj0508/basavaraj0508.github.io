export async function handler(event) {
  try {
    const q = event.queryStringParameters?.q || "devops";
    const location = event.queryStringParameters?.location || "United States";
    const limit = event.queryStringParameters?.limit || 20;

    // 🔹 Mock job data (replace later with Dice / Indeed APIs)
    const jobs = [
      {
        title: "Senior DevOps Engineer",
        company: "CloudScale Inc",
        location: "Remote - US",
        source: "Dice",
        url: "https://www.dice.com"
      },
      {
        title: "Platform Engineer",
        company: "NextGen Systems",
        location: "Austin, TX",
        source: "Indeed",
        url: "https://www.indeed.com"
      },
      {
        title: "SRE Engineer",
        company: "InfraWorks",
        location: "New York, NY",
        source: "LinkedIn",
        url: "https://www.linkedin.com"
      }
    ].slice(0, limit);

    return {
      statusCode: 200,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*"
      },
      body: JSON.stringify({
        query: q,
        location,
        count: jobs.length,
        jobs
      })
    };
  } catch (err) {
    return {
      statusCode: 500,
      body: JSON.stringify({ error: err.message })
    };
  }
}
