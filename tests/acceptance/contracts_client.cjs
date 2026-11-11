// Official JavaScript client against a disposable Pliqra workspace.
// The controller passes credentials through stdin, never arguments or files.
const OpenAI = require('openai');

async function main() {
  let source = '';
  for await (const chunk of process.stdin) source += chunk;
  const input = JSON.parse(source);
  const client = new OpenAI({
    apiKey: input.key,
    baseURL: input.baseURL,
    defaultHeaders: {'X-Pliqra-Binding': input.binding},
    maxRetries: 0,
  });
  const body = {model: input.model, messages: [{role: 'user', content: 'Say hello.'}], max_tokens: 24};
  if (input.expectedStatus === 409) {
    try {
      await client.chat.completions.create(body);
      throw new Error('Stale contract was accepted');
    } catch (error) {
      if (error.status !== 409) throw error;
    }
  } else {
    const result = await client.chat.completions.create(body);
    if (!result.choices[0].message.content) throw new Error('Missing real model content');
    let text = '';
    for await (const chunk of await client.chat.completions.create({...body, stream: true})) {
      text += chunk.choices[0]?.delta?.content || '';
    }
    if (!text) throw new Error('Missing streaming content');
  }
  process.stdout.write(JSON.stringify({passed: true}));
}

main().catch(error => {
  process.stderr.write(error.message);
  process.exitCode = 1;
});
